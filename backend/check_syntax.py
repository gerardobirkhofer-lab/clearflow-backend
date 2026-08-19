import ast, sys
files = [
    'app/core/config.py',
    'app/core/database.py',
    'app/models_orm.py',
    'app/schemas.py',
    'app/api/v1/tenants.py',
    'app/main.py',
]
ok = True
for f in files:
    try:
        with open(f) as fh:
            ast.parse(fh.read())
        print(f'OK: {f}')
    except Exception as e:
        print(f'FAIL: {f} -> {e}')
        ok = False
sys.exit(0 if ok else 1)
