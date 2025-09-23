import frappe
import requests
import json
from datetime import datetime
from frappe.utils import nowdate
from frappe.core.doctype.communication.email import make

def get_esignature_token():
    api_token = frappe.db.get_single_value("eSignature Settings","esignature_api_token")
    if not api_token:
        frappe.throw("E-signature API token not configured.")
    return api_token


@frappe.whitelist()
def get_esignature_templates():
    api_token = get_esignature_token()

    url = f"https://esignatures.com/api/templates?token={api_token}"
    response = requests.get(url)

    if response.status_code != 200:
        frappe.throw(f"Failed to fetch templates: {response.text}")

    templates = response.json().get("data", [])
    return [{"label": t["title"], "value": t["template_id"]} for t in templates]


@frappe.whitelist()
def send_for_signature(quotation_id, signer_name, signer_email):
    api_token = get_esignature_token()

    quotation = frappe.get_doc("Quotation", quotation_id)
    customer_name = quotation.customer_name or signer_name
    customer_email = quotation.contact_email or signer_email
    company = quotation.company
    template_raw = quotation.custom_esignature_template
    template_id = quotation.custom_esignature_template

    if not template_id:
        frappe.throw("No e-signature template selected.")
        
    create_url = f"https://esignatures.com/api/contracts?token={api_token}"

    payload = {
        "template_id": template_id,
        
        "signers": [{
            "signature_request_delivery_methods": [],
            "name": customer_name,
            "email": customer_email
        }],
        "placeholder_fields": [
            {"api_key": "quotation_id", "value": quotation.name},
            {"api_key": "signer_name", "value": customer_name},
            {"api_key": "signer_email", "value": customer_email},
            {"api_key": "company", "value": company},
            ]
        }


    create_response = requests.post(create_url, json=payload)
    if create_response.status_code != 200:
        frappe.throw(f"Failed to create contract: {create_response.text}")

    response_data = create_response.json()
    contract = response_data["data"]["contract"]
    custom_contract_id = contract["id"]
    custom_signing_url = contract["signers"][0]["sign_page_url"]

    pdf_attachment = frappe.attach_print(
        doctype = "Quotation",
        name = quotation.name, 
        file_name=f"{quotation.name}.pdf",
        print_format="Root Home Quotation"
        )

    frappe.sendmail(
        recipients=[customer_email],
        subject=f"Quotation {quotation.name} – Signature Request",
        message=f"""
            Dear {customer_name},<br><br>
            Please find your quotation attached.<br><br>
            To review and sign it, click the link below:<br>
            <a href="{custom_signing_url}">{custom_signing_url}</a><br><br>
            Best regards,<br>
            Root Home
        """,
        attachments=[pdf_attachment]
    )

    quotation.db_set("custom_signature_sent", 1)
    quotation.db_set("custom_contract_id", custom_contract_id)
    quotation.db_set("custom_signing_url", custom_signing_url)

    return {
        "status": "Email sent with quotation and signing link.",
        "custom_signing_url": custom_signing_url
    }
    
    
@frappe.whitelist(allow_guest=True)
def esignature_webhook():
    try:
        payload = frappe.local.request.get_json()
        if not payload:
            frappe.log_error("Webhook received but no payload (Empty JSON)", "Webhook Error")
            return {"error": "Invalid or empty JSON payload"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Webhook JSON Parsing Failed")
        return {"error": "Invalid JSON"}

    try:
        frappe.log_error("Webhook Triggered", "eSignature Webhook Debug")
        short_log = frappe.utils.cstr(str(payload)[:500])
        frappe.log_error(short_log, "Webhook Payload Summary")
    except Exception:
        pass

    expected_token = frappe.db.get_single_value("eSignature Settings", "esignature_api_token")
    if payload.get("secret_token") != expected_token:
        frappe.log_error("Invalid secret token in webhook", "Webhook Security")
        return {"error": "Unauthorized: Invalid webhook token"}

    if payload.get("status") != "contract-signed":
        return {"status": "Ignored", "reason": "Not a 'contract-signed' webhook"}

    contract = payload.get("data", {}).get("contract", {})
    if contract.get("status") != "signed":
        return {"status": "Ignored", "reason": "Contract not marked as signed"}

    contract_id = contract.get("id")
    custom_contract_id = contract.get("metadata") or contract_id
    pdf_url = contract.get("contract_pdf_url")
    timestamp = None

    signers = contract.get("signers", [])
    if signers:
        for event in signers[0].get("events", []):
            if event.get("event") == "sign_contract":
                timestamp = event.get("timestamp")
                break

    quotation_name = frappe.db.get_value("Quotation", {"custom_contract_id": custom_contract_id})
    if not quotation_name:
        return {
            "status": "error",
            "reason": f"No Quotation found for contract ID {custom_contract_id}"
        }

    frappe.db.set_value("Quotation", quotation_name, {
        "custom_signed_pdf_url": pdf_url,
        "custom_signature_date": timestamp[:10] if timestamp else nowdate(),
        "custom_document_signed": 1
    })
    message = f"The Quotation <b>{quotation_name}</b> has been signed."
    
    esign_users = frappe.get_all("Has Role", filters={"role": "e-signature"}, fields=["parent"])
    recipients = [u.parent for u in esign_users if frappe.get_value("User", u.parent, "enabled")]
    
    for user in recipients:
        notification = frappe.new_doc("Notification Log")
        notification.subject = f"Quotation {quotation_name} Signed"
        notification.email_content = message
        notification.for_user = user
        notification.type = "Alert"
        notification.document_type = "Quotation"
        notification.document_name = quotation_name
        notification.insert(ignore_permissions=True)

        
        make(
                subject=f"Quotation {quotation_name} Signed",
                content=message,
                recipients=[user],
                communication_type="Notification",
                send_email=True
            )
    return {
        "status": "success",
        "quotation": quotation_name,
        "contract_id": contract_id,
        "timestamp": timestamp
    }
