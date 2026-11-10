"""
Creates the TLS certificate that encrypts every connection to the servers.

    python tools/certificate.py            (create server.crt and server.key, or show the fingerprint)
    python tools/certificate.py --replace  (make a new pair, e.g. after changing a server address)

It prints the certificate's fingerprint. Give it to the people using gui.py: the
first time they connect, the GUI shows the server's fingerprint, and they should
only trust it if the two match.

The certificate is self-signed: rather than trusting a public certificate authority,
every client trusts exactly this certificate (server.crt in the project folder)
and refuses any server that presents a different one. It names every server host in
the settings (common.py), plus localhost and 127.0.0.1, so the clients' host name
check passes.

server.key is the private key. Only the servers need it, and it must stay secret.
server.crt is public: the command-line clients use it to check they reached the real server.

Needs the cryptography package: pip install -r requirements.txt
"""

import datetime
import hashlib
import ipaddress
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

# common.py is in the project folder, one level up from this one.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import users

VALID_DAYS = 365


def host_names(config: dict) -> list:
    """Every name the certificate must be valid for, as subject-alternative-name entries."""
    hosts = {"localhost", "127.0.0.1"} | {server["host"] for server in config["servers"].values()}
    names = []
    for host in sorted(hosts):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def create_certificate(config: dict) -> None:
    """Write a new key pair and self-signed certificate to the configured files."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Asset Management System server")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(host_names(config)), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    users.write_private_file(common.config_path(config, config["private_key_file"]), key_bytes.decode("ascii"))
    with open(common.config_path(config, config["certificate_file"]), "wb") as file:
        file.write(certificate.public_bytes(serialization.Encoding.PEM))


def fingerprint(config: dict) -> str:
    """The certificate's SHA-256 fingerprint, formatted the way gui.py shows it."""
    with open(common.config_path(config, config["certificate_file"]), "rb") as file:
        der = x509.load_pem_x509_certificate(file.read()).public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def main(arguments: list) -> None:
    config = common.load_config()
    certificate_path = common.config_path(config, config["certificate_file"])
    if os.path.exists(certificate_path) and "--replace" not in arguments:
        print(f"{config['certificate_file']} already exists. To make a new one, run: python tools/certificate.py --replace")
    else:
        create_certificate(config)
        print(f"Created {config['certificate_file']} and {config['private_key_file']}.")
    print(f"\nCertificate fingerprint (give this to GUI users to check):\n{fingerprint(config)}")


if __name__ == "__main__":
    main(sys.argv[1:])
