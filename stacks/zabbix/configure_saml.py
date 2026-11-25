#!/usr/bin/env python3
"""Idempotently configure Zabbix 8.0 LTS SAML 2.0 using Authentik's live metadata.

Run on ubuntu-desktop after running stacks/authentik/configure_saml_providers.py:
  python3 stacks/zabbix/configure_saml.py [sso_username]
Default sso_username is 'infra-admin' (local 'Admin' remains untouched as Break-glass).
"""

import subprocess
import sys
import textwrap
import urllib.request
import xml.etree.ElementTree as ET

METADATA_URL = "https://auth.home.example.com/application/saml/zabbix/metadata/"
SP_ENTITY_ID = "https://zabbix.home.example.com"
USERNAME_ATTR = "http://schemas.goauthentik.io/2021/02/saml/username"
NAMEID_FORMAT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"
REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"

NS = {
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}


def sql_literal(val: str) -> str:
    return "'" + val.replace("'", "''") + "'"


def fetch_metadata(url: str) -> dict[str, str]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        xml_bytes = resp.read()
    root = ET.fromstring(xml_bytes)

    entity_id = root.attrib["entityID"]

    sso_url = ""
    for el in root.findall(".//md:IDPSSODescriptor/md:SingleSignOnService", NS):
        if el.attrib.get("Binding") == REDIRECT_BINDING:
            sso_url = el.attrib["Location"]
            break

    slo_url = ""
    for el in root.findall(".//md:IDPSSODescriptor/md:SingleLogoutService", NS):
        if el.attrib.get("Binding") == REDIRECT_BINDING:
            slo_url = el.attrib["Location"]
            break

    cert_el = root.find(".//md:IDPSSODescriptor/md:KeyDescriptor[@use='signing']//ds:X509Certificate", NS)
    if cert_el is None:
        cert_el = root.find(".//md:IDPSSODescriptor//ds:X509Certificate", NS)
    if cert_el is None or not cert_el.text:
        raise RuntimeError("No X509Certificate found in Authentik SAML metadata")

    b64 = "".join(cert_el.text.split())
    pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(textwrap.wrap(b64, 64)) + "\n-----END CERTIFICATE-----\n"

    if not sso_url:
        raise RuntimeError("No HTTP-Redirect SingleSignOnService found in metadata")

    return {
        "entity_id": entity_id,
        "sso_url": sso_url,
        "slo_url": slo_url,
        "idp_cert_pem": pem,
    }


def main() -> None:
    sso_username = sys.argv[1] if len(sys.argv) > 1 else "infra-admin"
    meta = fetch_metadata(METADATA_URL)

    sql = f"""
BEGIN;

-- 1. Upsert Zabbix SAML userdirectory (idp_type = 2)
DO $$
DECLARE
    v_udid bigint;
    v_uid bigint;
    v_ugid bigint;
BEGIN
    SELECT userdirectoryid INTO v_udid FROM userdirectory WHERE idp_type = 2 LIMIT 1;
    IF v_udid IS NULL THEN
        SELECT COALESCE(MAX(userdirectoryid), 0) + 1 INTO v_udid FROM userdirectory;
        INSERT INTO userdirectory (userdirectoryid, name, description, idp_type, provision_status)
        VALUES (v_udid, 'Authentik SAML', '', 2, 0);
        INSERT INTO userdirectory_saml (
            userdirectoryid, idp_entityid, sso_url, slo_url, username_attribute,
            sp_entityid, nameid_format, sign_messages, sign_assertions,
            sign_authn_requests, sign_logout_requests, sign_logout_responses,
            encrypt_nameid, encrypt_assertions, idp_certificate
        ) VALUES (
            v_udid,
            {sql_literal(meta["entity_id"])},
            {sql_literal(meta["sso_url"])},
            {sql_literal(meta["slo_url"])},
            {sql_literal(USERNAME_ATTR)},
            {sql_literal(SP_ENTITY_ID)},
            {sql_literal(NAMEID_FORMAT)},
            1, 1, 0, 0, 0, 0, 0,
            {sql_literal(meta["idp_cert_pem"])}
        );
    ELSE
        UPDATE userdirectory_saml SET
            idp_entityid = {sql_literal(meta["entity_id"])},
            sso_url = {sql_literal(meta["sso_url"])},
            slo_url = {sql_literal(meta["slo_url"])},
            username_attribute = {sql_literal(USERNAME_ATTR)},
            sp_entityid = {sql_literal(SP_ENTITY_ID)},
            nameid_format = {sql_literal(NAMEID_FORMAT)},
            sign_messages = 1,
            sign_assertions = 1,
            sign_authn_requests = 0,
            sign_logout_requests = 0,
            sign_logout_responses = 0,
            encrypt_nameid = 0,
            encrypt_assertions = 0,
            idp_certificate = {sql_literal(meta["idp_cert_pem"])}
        WHERE userdirectoryid = v_udid;
    END IF;

    -- Keep ids table in sync for userdirectory
    INSERT INTO ids (table_name, field_name, nextid)
    VALUES ('userdirectory', 'userdirectoryid', v_udid)
    ON CONFLICT (table_name, field_name) DO UPDATE SET nextid = GREATEST(ids.nextid, EXCLUDED.nextid);

    -- 2. Enable SAML authentication (keeps local Admin break-glass via Internal group)
    UPDATE settings SET value_int = 1 WHERE name = 'saml_auth_enabled';

    -- 3. Ensure SSO super-admin user exists in Zabbix administrators (usrgrpid = 7, roleid = 3)
    SELECT userid INTO v_uid FROM users WHERE username = {sql_literal(sso_username)};
    IF v_uid IS NULL THEN
        SELECT COALESCE(MAX(userid), 0) + 1 INTO v_uid FROM users;
        INSERT INTO users (userid, username, name, surname, passwd, lang, roleid)
        VALUES (v_uid, {sql_literal(sso_username)}, {sql_literal(sso_username)}, '', '', 'ja_JP', 3);

        INSERT INTO ids (table_name, field_name, nextid)
        VALUES ('users', 'userid', v_uid)
        ON CONFLICT (table_name, field_name) DO UPDATE SET nextid = GREATEST(ids.nextid, EXCLUDED.nextid);
    END IF;

    IF NOT EXISTS (SELECT 1 FROM users_groups WHERE userid = v_uid AND usrgrpid = 7) THEN
        SELECT COALESCE(MAX(id), 0) + 1 INTO v_ugid FROM users_groups;
        INSERT INTO users_groups (id, usrgrpid, userid) VALUES (v_ugid, 7, v_uid);

        INSERT INTO ids (table_name, field_name, nextid)
        VALUES ('users_groups', 'id', v_ugid)
        ON CONFLICT (table_name, field_name) DO UPDATE SET nextid = GREATEST(ids.nextid, EXCLUDED.nextid);
    END IF;
END $$;

COMMIT;
"""

    subprocess.run(
        ["docker", "exec", "-i", "zabbix-postgres", "psql", "-U", "zabbix", "-d", "zabbix", "-v", "ON_ERROR_STOP=1"],
        input=sql,
        text=True,
        check=True,
    )
    print(f"OK: Zabbix 8.0 SAML configured (entity_id={meta['entity_id']}, sso_user={sso_username})")


if __name__ == "__main__":
    main()
