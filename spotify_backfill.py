import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import db, spotify as sp, json

config = json.loads((Path(__file__).parent / 'config.json').read_text())
CLIENT_ID     = config['spotify_client_id']
CLIENT_SECRET = config['spotify_client_secret']

db.init_db()
with db.get_db() as conn:
    rows = conn.execute(
        "SELECT id, artist, title FROM records WHERE spotify_url IS NULL OR spotify_url = ''"
    ).fetchall()

print(str(len(rows)) + " records need Spotify links")
found = 0
not_found = 0
for row in rows:
    url = sp.find_album_url(row['artist'], row['title'], CLIENT_ID, CLIENT_SECRET)
    if url:
        with db.get_db() as conn:
            conn.execute("UPDATE records SET spotify_url=? WHERE id=?", (url, row['id']))
        found += 1
        print("  OK  " + row['artist'] + " - " + row['title'])
    else:
        not_found += 1
        print("  --  " + row['artist'] + " - " + row['title'] + " (not found on Spotify)")
    time.sleep(0.3)

print("Done: " + str(found) + " linked, " + str(not_found) + " not found on Spotify")
