import clickhouse_connect
import os
from dotenv import load_dotenv

load_dotenv()
client = clickhouse_connect.get_client(
    host=os.getenv('CLICKHOUSE_HOST'),
    port=int(os.getenv('CLICKHOUSE_PORT', 8123)),
    database=os.getenv('CLICKHOUSE_DB', 'default'),
    username=os.getenv('CLICKHOUSE_USER'),
    password=os.getenv('CLICKHOUSE_PASSWORD')
)

print(client.query("SELECT count(*) FROM default.dm_Eyelash_copy WHERE Brand = 'No Brand'").result_rows)
