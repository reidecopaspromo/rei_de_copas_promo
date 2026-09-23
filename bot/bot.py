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
import io
import json
import base64
import logging
from datetime import datetime, timezone

import requests
from PIL import Image, ImageOps, ImageDraw
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
    "desconto_min": 20,       # em %
    "preco_min": 80,          # em R$ (sem teto máximo)
    "preco_max": None,        # None = sem limite superior
    "maximo_ofertas_na_pagina": 12,
    "limite_diario": 20,
}

PALAVRAS_CHAVE_PET = [
    "pet", "pets", "cão", "cachorro", "gato", "gata", "felino", "canino",
    "ração", "petisco", "coleira", "guia", "caixa de areia",
    "brinquedo pet", "casinha", "cama pet", "tapete higiênico",
    "shampoo pet", "tosa", "veterinário", "focinheira", "comedouro", "bebedouro",
    "antipulgas", "vermífugo", "arranhador", "transportadora",
    "peitoral", "guia retrátil", "escova pet", "removedor de pelos",
]

MARKETPLACES_CONFIAVEIS = [
    "amazon.com", "amzn.to",
    "mercadolivre.com", "meli.la", "mercadolibre.com",
    "shopee.com.br", "shope.ee",
]

ARQUIVO_CONTADOR = os.environ.get("GITHUB_CONTADOR_PATH", "contador_diario.json")

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


def avaliar(oferta, texto_original):
    if oferta["preco_original"] and oferta["preco_atual"]:
        desconto = round((oferta["preco_original"] - oferta["preco_atual"]) / oferta["preco_original"] * 100)
    else:
        desconto = 0
    motivos = []
    if desconto < CRITERIOS["desconto_min"]:
        motivos.append(f"desconto abaixo de {CRITERIOS['desconto_min']}%")
    preco = oferta["preco_atual"] or 0
    if preco < CRITERIOS["preco_min"]:
        motivos.append("preço abaixo do mínimo")
    if CRITERIOS["preco_max"] is not None and preco > CRITERIOS["preco_max"]:
        motivos.append("preço acima do máximo")
    if not oferta["link"]:
        motivos.append("sem link identificado")
    elif not any(dominio in oferta["link"].lower() for dominio in MARKETPLACES_CONFIAVEIS):
        motivos.append("marketplace não reconhecido como confiável")
    texto_lower = (texto_original or "").lower()
    if not any(palavra in texto_lower for palavra in PALAVRAS_CHAVE_PET):
        motivos.append("nenhuma palavra-chave de Pet encontrada")
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


def salvar_imagem(caminho, bytes_imagem):
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{caminho}"
    body = {
        "message": "Adiciona imagem de oferta (robô Rei de Copas)",
        "content": base64.b64encode(bytes_imagem).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    resp = requests.put(api, headers=github_headers(), json=body)
    resp.raise_for_status()
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{caminho}"


# Fundo creme e moldura dourada — mesma identidade visual da landing page.
COR_FUNDO_IMAGEM = (244, 241, 232)   # #F4F1E8
COR_MOLDURA = (214, 168, 79)         # #D6A84F
TAMANHO_IMAGEM = 800
ESPESSURA_MOLDURA = 6
MARGEM_INTERNA = 46


def processar_imagem(bytes_originais):
    """Recebe a foto crua do Telegram e devolve uma versão quadrada,
    com fundo creme e moldura dourada, pronta para a landing page."""
    img = Image.open(io.BytesIO(bytes_originais)).convert("RGB")

    canvas = Image.new("RGB", (TAMANHO_IMAGEM, TAMANHO_IMAGEM), COR_FUNDO_IMAGEM)

    area_util = TAMANHO_IMAGEM - 2 * MARGEM_INTERNA
    img_ajustada = ImageOps.contain(img, (area_util, area_util))

    pos_x = (TAMANHO_IMAGEM - img_ajustada.width) // 2
    pos_y = (TAMANHO_IMAGEM - img_ajustada.height) // 2
    canvas.paste(img_ajustada, (pos_x, pos_y))

    desenho = ImageDraw.Draw(canvas)
    metade = ESPESSURA_MOLDURA // 2
    desenho.rectangle(
        [metade, metade, TAMANHO_IMAGEM - metade - 1, TAMANHO_IMAGEM - metade - 1],
        outline=COR_MOLDURA,
        width=ESPESSURA_MOLDURA,
    )

    saida = io.BytesIO()
    canvas.save(saida, format="JPEG", quality=88)
    return saida.getvalue()


CONTADOR_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{ARQUIVO_CONTADOR}"


def carregar_contador_diario():
    resp = requests.get(CONTADOR_API, headers=github_headers(), params={"ref": GITHUB_BRANCH})
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if resp.status_code == 200:
        payload = resp.json()
        dados = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        if dados.get("data") != hoje:
            dados = {"data": hoje, "contagem": 0}
        return dados, payload["sha"]
    return {"data": hoje, "contagem": 0}, None


def salvar_contador_diario(dados, sha):
    conteudo = json.dumps(dados, ensure_ascii=False, indent=2)
    body = {
        "message": "Atualiza contador diário de ofertas (robô Rei de Copas)",
        "content": base64.b64encode(conteudo.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(CONTADOR_API, headers=github_headers(), json=body)
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
    desconto, aprovada, motivos = avaliar(oferta, texto)

    log.info("Mensagem recebida | aprovada=%s | motivos=%s | texto=%.60s", aprovada, motivos, texto)

    if not aprovada:
        return

    try:
        contador, sha_contador = carregar_contador_diario()
    except Exception as e:
        log.error("Falha ao ler contador diário no GitHub: %s", e)
        return

    if contador["contagem"] >= CRITERIOS["limite_diario"]:
        log.info("Limite diário de %s ofertas já atingido, ignorando.", CRITERIOS["limite_diario"])
        return

    try:
        ofertas, sha = carregar_ofertas_atuais()
    except Exception as e:
        log.error("Falha ao ler ofertas.json no GitHub: %s", e)
        return

    id_oferta = f"{msg.message_id}-{int(datetime.now(timezone.utc).timestamp())}"

    imagem_url = ""
    if msg.photo:
        try:
            maior_foto = msg.photo[-1]  # a última é sempre a de maior resolução
            arquivo = await context.bot.get_file(maior_foto.file_id)
            bytes_originais = bytes(await arquivo.download_as_bytearray())
            bytes_processados = processar_imagem(bytes_originais)
            imagem_url = salvar_imagem(f"imagens/{id_oferta}.jpg", bytes_processados)
        except Exception as e:
            log.error("Falha ao baixar/processar/salvar imagem da oferta: %s", e)

    nova = {
        "id": id_oferta,
        "nome": oferta["nome"] or "Oferta sem título",
        "preco_original": oferta["preco_original"],
        "preco_atual": oferta["preco_atual"],
        "desconto": desconto,
        "cupom": oferta["cupom"],
        "link": oferta["link"],
        "imagem": imagem_url,
        "cta_texto": CTA_TEXTO,
        "link_grupo": LINK_GRUPO_WHATSAPP,
        "capturado_em": datetime.now(timezone.utc).isoformat(),
    }

    ofertas.insert(0, nova)
    ofertas = ofertas[: CRITERIOS["maximo_ofertas_na_pagina"]]

    try:
        salvar_ofertas(ofertas, sha)
        log.info("Oferta aprovada e publicada: %s", nova["nome"])
        contador["contagem"] += 1
        salvar_contador_diario(contador, sha_contador)
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
