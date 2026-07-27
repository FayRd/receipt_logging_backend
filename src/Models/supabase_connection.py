import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

def get_supabase_connection() -> Client:
    supabase: Client = create_client(
        os.environ.get("SUPABASE_URL"),
        os.environ.get("SUPABASE_KEY")
    )

    return supabase