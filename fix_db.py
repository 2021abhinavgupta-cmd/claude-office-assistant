import sys
sys.path.insert(0, 'backend')
from db import get_connection

conn = get_connection()
cur = conn.cursor()

# Show tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cur.fetchall()
print('Tables:', [t[0] for t in tables])

# Show client_users rows
try:
    cur.execute('SELECT * FROM client_users')
    rows = cur.fetchall()
    print('\nclient_users rows:')
    for r in rows:
        print(' ', r)
except Exception as e:
    print('Error client_users:', e)

# Show clients rows
try:
    cur.execute("SELECT id, name FROM clients")
    rows = cur.fetchall()
    print('\nclients rows:')
    for r in rows:
        print(' ', r)
except Exception as e:
    print('Error clients:', e)

# Delete the mellow user
try:
    cur.execute("DELETE FROM client_users WHERE username='mellow'")
    conn.commit()
    print(f'\nDeleted {cur.rowcount} client_users with username=mellow')
except Exception as e:
    print('Error deleting mellow:', e)

conn.close()
