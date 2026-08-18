import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_cors = '''    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://localhost:5173",
            "https://clearflow-demo.vercel.app",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )'''

new_cors = '''    # CORS
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
    allow_origins = [
        "http://localhost:3000",
        "http://localhost:5173",
        "https://clearflow-demo.vercel.app",
    ]
    if frontend_url and frontend_url not in allow_origins:
        allow_origins.append(frontend_url)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )'''

if old_cors in content:
    content = content.replace(old_cors, new_cors)
    print("✅ CORS reemplazado correctamente")
else:
    print("⚠️ No se encontró el bloque CORS exacto. Revisá el archivo manualmente.")

# Asegurar que import os esté presente
if 'import os' not in content:
    # Buscar la primera línea de imports y agregar import os ahí
    content = 'import os\n' + content
    print("✅ Agregado 'import os'")

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)
