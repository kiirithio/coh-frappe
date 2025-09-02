import frappe
import json
import requests
import base64
from frappe.utils import now_datetime


# --------- HELPER: Load Settings ----------
def get_mpesa_settings():
    return frappe.get_single("M-Pesa Settings")


# --------- HELPER: Get OAuth Token ----------
def get_access_token(consumer_key, consumer_secret, base_url):
    auth = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    url = f"{base_url}/oauth/v1/generate?grant_type=client_credentials"

    res = requests.get(url, headers=headers)
    res.raise_for_status()
    return res.json()["access_token"]


# --------- CREATE DONATION & TRIGGER STK ----------
@frappe.whitelist(allow_guest=True)
def create_donation():
    data = frappe.form_dict
    settings = get_mpesa_settings()

    donation = frappe.get_doc({
        "doctype": "Donation",
        "donor_name": data.get("donor_name"),
        "phone_number": data.get("phone_number"),
        "email": data.get("email"),
        "donation_amount": data.get("donation_amount"),
        "mpesa_transaction_id": data.get("mpesa_transaction_id"),
        "reference": data.get("reference"),
    })
    donation.insert(ignore_permissions=True)
    frappe.db.commit()

    # STK Push
    try:
        token = get_access_token(
            settings.consumer_key,
            settings.consumer_secret,
            settings.base_url or "https://api.safaricom.co.ke"
        )

        timestamp = now_datetime().strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(
            f"{settings.shortcode}{settings.passkey}{timestamp}".encode()
        ).decode()

        stk_url = f"{settings.base_url}/mpesa/stkpush/v1/processrequest"
        payload = {
            "BusinessShortCode": settings.shortcode,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(data.get("donation_amount")),
            "PartyA": data.get("phone_number"),
            "PartyB": settings.shortcode,
            "PhoneNumber": data.get("phone_number"),
            "CallBackURL": settings.callback_url,
            "AccountReference": data.get("reference") or "Donation",
            "TransactionDesc": "Donation Payment"
        }

        headers = {"Authorization": f"Bearer {token}"}
        res = requests.post(stk_url, json=payload, headers=headers)
        res.raise_for_status()
        response_data = res.json()
    except Exception as e:
        frappe.log_error(f"STK Push Error: {str(e)}", "M-Pesa STK Push")
        response_data = {"error": str(e)}

    return {
        "message": "Donation recorded successfully",
        "donation_id": donation.name,
        "stk_push_response": response_data
    }


# --------- CALLBACK HANDLER ----------
@frappe.whitelist(allow_guest=True)
def mpesa_callback():
    try:
        data = json.loads(frappe.request.data or "{}")
        stk_callback = data.get("Body", {}).get("stkCallback", {})

        metadata = {item["Name"]: item.get("Value") for item in stk_callback.get("CallbackMetadata", {}).get("Item", [])}

        log = frappe.get_doc({
            "doctype": "M-Pesa Payment Log",
            "transaction_id": metadata.get("MpesaReceiptNumber"),
            "phone_number": metadata.get("PhoneNumber"),
            "amount": metadata.get("Amount"),
            "organization_balance": None,
            "transaction_type": "Paybill",
            "account_reference": metadata.get("AccountReference"),
            "description": stk_callback.get("ResultDesc"),
            "raw_callback": json.dumps(data, indent=2),
            "status": "Success" if stk_callback.get("ResultCode") == 0 else "Failed",
            "date_received": now_datetime()
        })
        log.insert(ignore_permissions=True)
        frappe.db.commit()

        return {"ResultCode": 0, "ResultDesc": "Callback received successfully"}
    except Exception as e:
        frappe.log_error(f"Callback Error: {str(e)}", "M-Pesa Callback")
        return {"ResultCode": 1, "ResultDesc": "Error processing callback"}
