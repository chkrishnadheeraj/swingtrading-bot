import sys
import notion_client

client = notion_client.Client(auth="ntn_E679779092745M6JiQWUbxcGj7TwMAEsNTWXGnjEwMp8Tg")
db_id = "3321952e0ecf80f7b92dc817cd04713c"

try:
    db = client.databases.retrieve(database_id=db_id)
    title_prop_name = None
    for k, v in db["properties"].items():
        if v["type"] == "title":
            title_prop_name = k
            break
            
    properties = {
        title_prop_name: {"name": "ID"},
        "Stock": {"rich_text": {}},
        "Status": {
            "select": {
                "options": [
                    {"name": "OPEN", "color": "blue"},
                    {"name": "WIN", "color": "green"},
                    {"name": "LOSS", "color": "red"}
                ]
            }
        },
        "Action": {"select": {}},
        "Quantity": {"number": {"format": "number"}},
        "Entry Price": {"number": {"format": "rupee"}},
        "Stop Loss": {"number": {"format": "rupee"}},
        "Target Price": {"number": {"format": "rupee"}},
        "Strategy": {"select": {}},
        "Entry Time": {"date": {}},
        "Reason": {"rich_text": {}},
        "Mode": {"select": {}},
        "Value at Risk": {"number": {"format": "rupee"}},
        "Exit Price": {"number": {"format": "rupee"}},
        "P&L": {"number": {"format": "rupee"}},
        "P&L %": {"number": {"format": "percent"}},
        "Exit Reason": {"rich_text": {}},
        "Exit Time": {"date": {}},
        "Hold Days": {"number": {}}
    }
    
    print("Updating Notion database schema...")
    client.databases.update(
        database_id=db_id,
        properties=properties
    )
    print("✅ Schema created successfully")
    
except Exception as e:
    print(f"❌ Failed to update schema: {e}")
    sys.exit(1)
