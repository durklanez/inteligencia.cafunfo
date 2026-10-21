from flask import Flask, request, jsonify, send_from_directory, session, redirect
from flask_cors import CORS
import os
import time
import requests
from groq import Groq
import firebase_admin
from firebase_admin import credentials, auth

# ==========================================
# FLASK
# ==========================================
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# Configure FLASK_SECRET_KEY nas variáveis de ambiente do Render.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax"
)

# ==========================================
# FIREBASE
# ==========================================
FIREBASE_CREDENTIALS = os.environ.get(
    "FIREBASE_CREDENTIALS",
    "/etc/secrets/firebase-credentials.json"
)
FIREBASE_WEB_API_KEY = os.environ.get("FIREBASE_WEB_API_KEY")
firebase_ok = False

try:
    if os.path.exists(FIREBASE_CREDENTIALS):
        if not firebase_admin._apps:
            cred = credentials.Certificate(FIREBASE_CREDENTIALS)
            firebase_admin.initialize_app(cred)
        firebase_ok = True
        print("FIREBASE INICIALIZADO COM SUCESSO")
    else:
        print("ERRO FIREBASE: arquivo não encontrado:", FIREBASE_CREDENTIALS)
except Exception as e:
    print("ERRO AO INICIALIZAR FIREBASE:", type(e).__name__, repr(e))

# ==========================================
# GROQ
# ==========================================
api_key = os.environ.get("GROQ_API_KEY")
groq_model = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")
client = None

if api_key:
    try:
        client = Groq(api_key=api_key, timeout=30.0)
        print("GROQ INICIALIZADO. MODELO:", groq_model)
    except Exception as e:
        print("ERRO AO INICIALIZAR GROQ:", type(e).__name__, repr(e))
else:
    print("ERRO: GROQ_API_KEY NÃO ENCONTRADA")

# ==========================================
# TRATAMENTO DE ERROS
# ==========================================
def erro_temporario(erro):
    texto = str(erro).upper()
    nome = type(erro).__name__.upper()
    return (
        "503" in texto
        or "SERVICE_UNAVAILABLE" in texto
        or "TIMEOUT" in texto
        or "READTIMEOUT" in nome
        or "CONNECTTIMEOUT" in nome
        or "REMOTEPROTOCOLERROR" in nome
    )


def erro_rate_limit(erro):
    texto = str(erro).upper()
    nome = type(erro).__name__.upper()
    return (
        "429" in texto
        or "RATE_LIMIT" in texto
        or "RATELIMIT" in texto
        or "TOO MANY REQUESTS" in texto
        or "RESOURCE_EXHAUSTED" in texto
        or "RATE" in nome
    )


def chamar_groq(messages):
    ultimo_erro = None

    for tentativa in range(1, 3):
        inicio = time.time()
        try:
            print(f"Chamada Groq {tentativa}/2 | modelo: {groq_model}")
            response = client.chat.completions.create(
                model=groq_model,
                messages=messages,
                temperature=0.7,
                max_tokens=2048
            )
            print("GROQ RESPONDEU EM", round(time.time() - inicio, 2), "SEGUNDOS")
            return response
        except Exception as e:
            ultimo_erro = e
            print(
                "ERRO GROQ:", type(e).__name__, repr(e),
                "| tempo:", round(time.time() - inicio, 2)
            )
            if erro_rate_limit(e):
                break
            if tentativa < 2 and erro_temporario(e):
                time.sleep(0.5)
                continue
            break

    if ultimo_erro:
        raise ultimo_erro
    raise RuntimeError("A chamada à Groq falhou sem erro específico.")

# ==========================================
# SESSÃO
# ==========================================
def criar_sessao_usuario(decoded_token):
    session["uid"] = decoded_token["uid"]
    session["email"] = decoded_token.get("email") or ""
    session["nome"] = decoded_token.get("name") or ""
    return True

# ==========================================
# PÁGINA INICIAL
# ==========================================
@app.route("/")
def home():
    return send_from_directory(".", "register.htm")

# ==========================================
# REGISTO
# ==========================================
@app.route("/register", methods=["POST", "OPTIONS"])
def register():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"erro": "Dados inválidos."}), 400

        nome = str(data.get("nome", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        senha = str(data.get("senha", ""))

        if not email or not senha:
            return jsonify({"erro": "Email e senha são obrigatórios."}), 400
        if len(senha) < 6:
            return jsonify({"erro": "A senha precisa ter pelo menos 6 caracteres."}), 400
        if not firebase_ok:
            return jsonify({"erro": "Firebase não está configurado no servidor."}), 500

        usuario = auth.create_user(
            email=email,
            password=senha,
            display_name=nome if nome else None
        )
        print("NOVO USUÁRIO FIREBASE:", usuario.uid, usuario.email)

        return jsonify({
            "ok": True,
            "mensagem": "Conta criada com sucesso.",
            "uid": usuario.uid,
            "email": usuario.email,
            "nome": usuario.display_name or nome
        }), 201

    except Exception as e:
        print("ERRO NO REGISTER:", type(e).__name__, repr(e))
        texto = str(e).upper()
        if "EMAIL_EXISTS" in texto or "EMAIL ALREADY EXISTS" in texto:
            mensagem = "Este email já está cadastrado."
        else:
            mensagem = "Não foi possível criar a conta."
        return jsonify({"erro": mensagem}), 400

# ==========================================
# LOGIN
# ==========================================
@app.route("/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"erro": "Dados inválidos."}), 400

        email = str(data.get("email", "")).strip().lower()
        senha = str(data.get("senha", ""))

        if not email or not senha:
            return jsonify({"erro": "Email e senha são obrigatórios."}), 400
        if not FIREBASE_WEB_API_KEY:
            return jsonify({"erro": "Firebase Web API Key não configurada."}), 500

        url = (
            "https://identitytoolkit.googleapis.com/"
            "v1/accounts:signInWithPassword?key=" + FIREBASE_WEB_API_KEY
        )
        resposta = requests.post(
            url,
            json={
                "email": email,
                "password": senha,
                "returnSecureToken": True
            },
            timeout=15
        )
        resultado = resposta.json()

        if resposta.status_code != 200:
            print("ERRO LOGIN FIREBASE:", resposta.status_code, resultado)
            mensagem = resultado.get("error", {}).get("message", "")
            mensagens = {
                "EMAIL_NOT_FOUND": "Email ou senha incorretos.",
                "INVALID_PASSWORD": "Email ou senha incorretos.",
                "INVALID_LOGIN_CREDENTIALS": "Email ou senha incorretos.",
                "USER_DISABLED": "Esta conta está desativada."
            }
            return jsonify({
                "erro": mensagens.get(mensagem, "Não foi possível entrar.")
            }), 401

        id_token = resultado.get("idToken")
        if not id_token:
            return jsonify({"erro": "Firebase não retornou o token."}), 500

        decoded_token = auth.verify_id_token(id_token)
        criar_sessao_usuario(decoded_token)
        nome = decoded_token.get("name") or ""

        print("LOGIN REALIZADO:", decoded_token["uid"], email)
        return jsonify({
            "ok": True,
            "mensagem": "Login realizado com sucesso.",
            "uid": decoded_token["uid"],
            "email": email,
            "nome": nome,
            "usuario": {
                "uid": decoded_token["uid"],
                "email": email,
                "nome": nome
            }
        })

    except Exception as e:
        print("ERRO NO LOGIN:", type(e).__name__, repr(e))
        return jsonify({"erro": "Erro ao conectar com o Firebase."}), 500

# ==========================================
# UTILIZADOR ATUAL
# ==========================================
@app.route("/me", methods=["GET"])
def me():
    if "uid" not in session:
        return jsonify({"autenticado": False}), 401
    return jsonify({
        "autenticado": True,
        "uid": session.get("uid"),
        "email": session.get("email"),
        "nome": session.get("nome")
    })

# ==========================================
# LOGOUT
# ==========================================
@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return jsonify({"ok": True, "mensagem": "Sessão encerrada."})

# ==========================================
# PÁGINA DE TRABALHO
# ==========================================
@app.route("/trabalhar")
def trabalhar():
    if "uid" not in session:
        return redirect("/")
    return send_from_directory(".", "index.html")

# ==========================================
# TESTE DO SERVIDOR
# ==========================================
@app.route("/teste")
def teste():
    return jsonify({
        "status": "online",
        "groq": client is not None,
        "modelo": groq_model,
        "firebase": firebase_ok,
        "firebase_api_key": bool(FIREBASE_WEB_API_KEY)
    })

# ==========================================
# MAPA DE LINGUAGENS
# ==========================================
MAPA_LINGUAGENS = {
    "javascript": "js",
    "js": "js",
    "html": "html",
    "htm": "html",
    "css": "css",
    "python": "py",
    "py": "py",
    "typescript": "ts",
    "ts": "ts",
    "json": "json",
    "java": "java",
    "php": "php",
    "sql": "sql",
    "c": "c",
    "cpp": "cpp",
    "c++": "cpp",
    "dart": "dart",
    "flutter": "dart",
    "yaml": "yaml",
    "yml": "yaml",
    "kotlin": "kt",
    "kt": "kt",
    "swift": "swift",
    "xml": "xml",
    "bash": "sh",
    "shell": "sh",
    "text": "txt",
    "plaintext": "txt"
}


def extrair_codigo(resposta):
    """Extrai o primeiro bloco Markdown e identifica a linguagem."""
    if not resposta or "```" not in resposta:
        return "", "txt"

    partes = resposta.split("```")
    if len(partes) < 3:
        return "", "txt"

    bloco = partes[1].strip()
    if not bloco:
        return "", "txt"

    linhas = bloco.splitlines()
    identificador = linhas[0].strip().lower() if linhas else ""
    linguagem = identificador.split()[0] if identificador else ""

    if linguagem in MAPA_LINGUAGENS:
        tipo = MAPA_LINGUAGENS[linguagem]
        codigo = "\n".join(linhas[1:]).strip()
    else:
        tipo = "txt"
        codigo = bloco

    return codigo, tipo

# ==========================================
# API ELI
# ==========================================
@app.route("/teste-firestore", methods=["POST", "OPTIONS"])
def teste_firestore():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}

        pergunta = str(data.get("pergunta", "")).strip()
        historico = data.get("historico", [])

        if not pergunta:
            return jsonify({
                "resposta": "Digite uma mensagem para a Eli.",
                "codigo": "",
                "tipo": "txt"
            })

        if client is None:
            return jsonify({
                "resposta": "❌ GROQ_API_KEY não está configurada no Render.",
                "codigo": "",
                "tipo": "txt",
                "modelo": groq_model
            }), 500

        messages = [{
            "role": "system",
            "content": (
                "Você é a Eli AI, assistente do Inteligência Cafunfo. "
                "Responda em português, de forma clara e útil. Ajude com programação "
                "e criação de aplicativos. Quando o utilizador pedir código, use blocos "
                "Markdown com o identificador correto, como ```dart, ```yaml, ```html, "
                "```css ou ```python. Quando pedirem um projeto Flutter, explique que "
                "um projeto completo pode precisar de vários ficheiros, como lib/main.dart "
                "e pubspec.yaml. Nunca diga que compilou um APK se isso não aconteceu."
            )
        }]

        if isinstance(historico, list):
            for msg in historico[-6:]:
                if not isinstance(msg, dict):
                    continue
                texto = msg.get("content")
                if not texto:
                    continue
                role = "assistant" if msg.get("role") == "assistant" else "user"
                messages.append({"role": role, "content": str(texto)})

        if (
            len(messages) == 1
            or messages[-1]["role"] != "user"
            or messages[-1]["content"] != pergunta
        ):
            messages.append({"role": "user", "content": pergunta})

        print("NOVA PERGUNTA:", pergunta, "| modelo:", groq_model)
        response = chamar_groq(messages)

        try:
            resposta = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            resposta = None

        if not resposta:
            resposta = "Não consegui gerar uma resposta agora."

        codigo, tipo = extrair_codigo(resposta)
        print("TIPO DE CÓDIGO:", tipo, "| código encontrado:", bool(codigo))

        return jsonify({
            "resposta": resposta,
            "codigo": codigo,
            "tipo": tipo,
            "modelo": groq_model
        })

    except Exception as e:
        erro = repr(e)
        print("ERRO GROQ/SERVIDOR:", type(e).__name__, erro)

        if erro_rate_limit(e):
            return jsonify({
                "resposta": (
                    "⚠️ O limite da API Groq foi atingido. "
                    "Aguarda o limite renovar e tenta novamente."
                ),
                "codigo": "",
                "tipo": "txt",
                "modelo": groq_model
            }), 429

        if erro_temporario(e):
            return jsonify({
                "resposta": "⏳ A Groq demorou para responder. Tenta novamente.",
                "codigo": "",
                "tipo": "txt",
                "modelo": groq_model
            }), 503

        return jsonify({
            "resposta": "❌ Erro no servidor.",
            "codigo": "",
            "tipo": "txt",
            "erro": erro,
            "modelo": groq_model
        }), 500

# ==========================================
# INICIAR
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    print("SERVIDOR ELI AI INICIANDO NA PORTA:", port)
    app.run(host="0.0.0.0", port=port)
