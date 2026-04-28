"""
Run once on the Pi to generate a self-signed TLS certificate for HTTPS.
The cert includes Subject Alternative Names (SANs) required by modern
mobile browsers (Chrome on Android, Safari on iOS).

Usage:
    python generate_cert.py
"""
import socket
from OpenSSL import crypto
from pathlib import Path

CERT_FILE = Path(__file__).parent / "cert.pem"
KEY_FILE  = Path(__file__).parent / "key.pem"


def get_local_ip():
    """Best-effort: get the Pi's LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def generate():
    local_ip = get_local_ip()

    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)

    cert = crypto.X509()
    cert.get_subject().CN = local_ip
    cert.set_serial_number(1)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)  # 10 years
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)

    # Subject Alternative Names — required by Chrome/Safari on mobile.
    # Includes the LAN IP and localhost for convenience.
    san = f"IP:{local_ip}, IP:127.0.0.1, DNS:localhost, DNS:vinyl-inventory"
    cert.add_extensions([
        crypto.X509Extension(b"subjectAltName", False, san.encode()),
        crypto.X509Extension(b"basicConstraints", True, b"CA:FALSE"),
        crypto.X509Extension(b"keyUsage", True,
                             b"digitalSignature, keyEncipherment"),
        crypto.X509Extension(b"extendedKeyUsage", False, b"serverAuth"),
    ])

    cert.sign(k, "sha256")

    with open(CERT_FILE, "wb") as f:
        f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))
    with open(KEY_FILE, "wb") as f:
        f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))

    print(f"Certificate generated for IP: {local_ip}")
    print(f"  {CERT_FILE}")
    print(f"  {KEY_FILE}")
    print()
    print("On your phone, open Chrome and go to:")
    print(f"  https://{local_ip}:5001")
    print("Tap 'Advanced' → 'Proceed to site (unsafe)'")
    print("This only needs to be done once.")


if __name__ == "__main__":
    generate()
