"""Controlla l'accessibilità dei path delle foto."""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.mariadb_store import MariaDBStore

db = MariaDBStore()
try:
    rows = db._query("SELECT path FROM file_registry WHERE path LIKE %s LIMIT 5", ("%OldMemory%",))
    for r in rows:
        p = r["path"]
        print(f"Path DB: {repr(p)}")
        print(f"  Esiste: {os.path.isfile(p)}")
        print(f"  Drive: {os.path.splitdrive(p)}")
        
        # Prova a listare la directory
        parent = os.path.dirname(p)
        if os.path.isdir(parent):
            print(f"  Directory OK: {parent}")
            print(f"  Contenuto: {os.listdir(parent)[:5]}")
        else:
            print(f"  Directory NON accessibile: {parent}")
            
            # Verifica se Z: esiste
            print(f"  Z: esiste: {os.path.exists('Z:')}")
            print(f"  Z: è mount: {os.path.ismount('Z:')}")
            if os.path.exists('Z:'):
                print(f"  Contenuto Z: {os.listdir('Z:')[:10]}")
    
    # Verifica Z: drive
    print(f"\n--- Z: drive ---")
    print(f"  Esiste: {os.path.exists('Z:')}")
    if os.path.exists('Z:'):
        for item in os.listdir('Z:')[:20]:
            print(f"  {item}")
    
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    db.close()
