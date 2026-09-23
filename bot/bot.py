"""
Rei de Copas Promo — Robô de curadoria via Telegram

O QUE ESTE ROBÔ FAZ:
1. Fica "ouvindo" o canal do Telegram onde a Lumi posta as ofertas.
2. Para cada mensagem nova, extrai nome, preço original, preço atual, cupom e link.
3. Aplica os critérios definidos abaixo (CRITERIOS).
4. Se a oferta passar, adiciona ela no arquivo ofertas.json e sobe esse
   arquivo para um repositório no GitHub.
5. Sua landing page lê esse ofertas.json direto do GitHub e
   mostra as ofertas aprovadas — sem você mexer em nada.

ESTE ARQUIVO PRECISA FICAR RODANDO O TEMPO TODO EM ALGUM SERVIDOR.
Não roda no seu celular nem numa aba do navegador. Veja o README.md
nesta mesma pasta para o passo a passo de colocar isso no ar de graça
(Railway ou Render).
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone

import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rei-de-copas-bot")

# ----------------------------------------------------------------------
# CONFIGURAÇÃO — preenchida via variáveis de ambiente no Railway
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")  # ex: -1003969496525
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "seu-usuario/rei-de-copas-site")
GITHUB_FILE_PATH = os.environ.get("GITHUB_FILE_PATH", "ofertas.json")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# Critérios de aprovação — ajuste livremente.
CRITERIOS = {
    "desconto_min": 30,       # em %
    "preco_min": 0,           # em R$
    "preco_max": 500,         # em R$
    "categorias_permitidas": ["Pet", "Kids"],
    "maximo_ofertas_na_pagina": 12,
}

CTA_TEXTO = "Quer receber essa e muitas outras ofertas em primeira mão? Entre no nosso grupo:"
LINK_GRUPO_WHATSAPP = os.environ.get("LINK_GRUPO_WHATSAPP", "https://chat.whatsapp.com/SEU-LINK-AQUI")

# ----------------------------------------------------------------------
# EXTRAÇÃO DE TEXTO
# ----------------------------------------------------------------------

def numero_br(s):
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def extrair_oferta(texto):
    resultado = {"nome": "", "preco_original": None, "preco_atual": None, "link": "", "cupom": ""}
    if not texto:
        return resultado

    link_match = re.search(r"(https?://\S+)", texto, re.IGNORECASE)
    if link_match:
        resultado["link"] = link_match.group(1)

    de_para = re.search(r"de\s*R\$\s*([\d.,]+)\s*por\s*R\$\s*([\d.,]+)", texto, re.IGNORECASE)
    if de_para:
        resultado["preco_original"] = numero_br(de_para.group(1))
        resultado["preco_atual"] = numero_br(de_para.group(2))
    else:
        precos = [numero_br(p) for p in re.findall(r"R\$\s*([\d.,]+)", texto, re.IGNORECASE)]
        precos = [p for p in precos if p is not None]
        if len(precos) >= 2:
            resultado["preco_original"] = max(precos)
            resultado["preco_atual"] = min(precos)
        elif len(precos) == 1:
            resultado["preco_atual"] = precos[0]

    cupom_match = re.search(r"cupom:?\s*([A-Z0-9]{3,})", texto, re.IGNORECASE)
    if cupom_match:
        resultado["cupom"] = cupom_match.group(1)

    linhas = [
        l.strip()
        for l in texto.split("\n")
        if l.strip() and not re.match(r"^(de\s*R\$|R\$|cupom|https?://|loja oficial)", l.strip(), re.IGNORECASE)
    ]
    if linhas:
        resultado["nome"] = " — ".join(linhas[:2])

    return resultado


def avaliar(oferta):
    if oferta["preco_original"] and oferta["preco_atual"]:
        desconto = round((oferta["preco_original"] - oferta["preco_atual"]) / oferta["preco_original"] * 100)
    else:
        desconto = 0
    motivos = []
    if desconto < CRITERIOS["desconto_min"]:
        motivos.append(f"desconto abaixo de {CRITERIOS['desconto_min']}%")
    preco = oferta["preco_atual"] or 0
    if preco < CRITERIOS["preco_min"] or preco > CRITERIOS["preco_max"]:
        motivos.append("fora da faixa de preço")
    if not oferta["link"]:
        motivos.append("sem link identificado")
    return desconto, len(motivos) == 0, motivos

# ----------------------------------------------------------------------
# GITHUB
# ----------------------------------------------------------------------

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"


def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def carregar_ofertas_atuais():
    resp = requests.get(GITHUB_API, headers=github_headers(), params={"ref": GITHUB_BRANCH})
    if resp.status_code == 200:
        payload = resp.json()
        conteudo = base64.b64decode(payload["content"]).decode("utf-8")
        return json.loads(conteudo), payload["sha"]
    if resp.status_code == 404:
        return [], None
    resp.raise_for_status()


def salvar_ofertas(ofertas, sha):
    conteudo = json.dumps(ofertas, ensure_ascii=False, indent=2)
    body = {
        "message": "Atualiza ofertas aprovadas (robô Rei de Copas)",
        "content": base64.b64encode(conteudo.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(GITHUB_API, headers=github_headers(), json=body)
    resp.raise_for_status()

# ----------------------------------------------------------------------
# HANDLER DO TELEGRAM
# ----------------------------------------------------------------------

async def nova_mensagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    if CHANNEL_ID and str(msg.chat_id) != str(CHANNEL_ID):
        return

    texto = msg.text or msg.caption
    if not texto:
        return

    oferta = extrair_oferta(texto)
    desconto, aprovada, motivos = avaliar(oferta)

    log.info("Mensagem recebida | aprovada=%s | motivos=%s | texto=%.60s", aprovada, motivos, texto)

    if not aprovada:
        return

    try:
        ofertas, sha = carregar_ofertas_atuais()
    except Exception as e:
        log.error("Falha ao ler ofertas.json no GitHub: %s", e)
        return

    nova = {
        "id": f"{msg.message_id}-{int(datetime.now(timezone.utc).timestamp())}",
        "nome": oferta["nome"] or "Oferta sem título",
        "preco_original": oferta["preco_original"],
        "preco_atual": oferta["preco_atual"],
        "desconto": desconto,
        "cupom": oferta["cupom"],
        "link": oferta["link"],
        "cta_texto": CTA_TEXTO,
        "link_grupo": LINK_GRUPO_WHATSAPP,
        "capturado_em": datetime.now(timezone.utc).isoformat(),
    }

    ofertas.insert(0, nova)
    ofertas = ofertas[: CRITERIOS["maximo_ofertas_na_pagina"]]

    try:
        salvar_ofertas(ofertas, sha)
        log.info("Oferta aprovada e publicada: %s", nova["nome"])
    except Exception as e:
        log.error("Falha ao salvar ofertas.json no GitHub: %s", e)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Defina a variável de ambiente TELEGRAM_BOT_TOKEN antes de iniciar.")
    if not GITHUB_TOKEN:
        raise SystemExit("Defina a variável de ambiente GITHUB_TOKEN antes de iniciar.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & (~filters.COMMAND), nova_mensagem))

    log.info("Robô no ar, escutando o canal do Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
