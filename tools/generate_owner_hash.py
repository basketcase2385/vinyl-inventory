from getpass import getpass
from werkzeug.security import generate_password_hash


def main():
    pw = getpass("Owner password: ").strip()
    confirm = getpass("Confirm password: ").strip()
    if not pw or pw != confirm:
        raise SystemExit("Passwords do not match or are empty.")
    print(generate_password_hash(pw))


if __name__ == "__main__":
    main()
