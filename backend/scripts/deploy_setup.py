"""Build-time configuration validation. Schema changes belong to Supabase migrations."""
from app.database import settings


def main():
    settings()
    print("Supabase configuration present. Apply checked-in SQL migrations through Supabase before deployment.")


if __name__ == "__main__":
    main()
