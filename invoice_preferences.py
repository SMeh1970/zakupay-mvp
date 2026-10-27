"""Durable account defaults; invoice snapshots always take precedence."""
import copy
import json
from invoice_generator import validate_seller

SCHEMA = "CREATE TABLE IF NOT EXISTS panel_invoice_preferences (username TEXT PRIMARY KEY, settings_json TEXT NOT NULL, enabled INTEGER NOT NULL)"


def load(conn, execute, username, include_disabled=False):
    row = execute(conn, "SELECT settings_json, enabled FROM panel_invoice_preferences WHERE username=?", (username,)).fetchone()
    if not row or (not row['enabled'] and not include_disabled):
        return None
    value = json.loads(row['settings_json'])
    value['seller'] = validate_seller(value['seller'])
    return value


def save(conn, execute, username, settings):
    if settings is None:
        execute(conn, "UPDATE panel_invoice_preferences SET enabled=0 WHERE username=?", (username,))
    else:
        execute(conn, "INSERT INTO panel_invoice_preferences (username, settings_json, enabled) VALUES (?, ?, 1) ON CONFLICT(username) DO UPDATE SET settings_json=excluded.settings_json, enabled=1", (username, json.dumps(settings, ensure_ascii=False)))


def apply(result, preference):
    if not preference or result.get('seller') or result.get('live_offer_created') or result.get('invoice_seller_confirmed_at'):
        return result
    result = copy.deepcopy(result)
    result.update(seller=copy.deepcopy(preference['seller']), vat_rate=preference['vat_rate'], destination_account_id='')
    return result


def selected_profile(result, key, profiles, conn, execute, username):
    profile = profiles.get(key)
    # Unpinning expands the controls and retains their current choice for this invoice.
    if profile is None and key == 'saved' and not result.get('seller'):
        previous = load(conn, execute, username, include_disabled=True)
        profile = previous['seller'] if previous else None
    return profile
