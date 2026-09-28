"""
Rei de Copas Promo — Robô de curadoria via Telegram (4 nichos)

O QUE ESTE ROBÔ FAZ:
1. Fica "ouvindo" 4 canais privados do Telegram, um por nicho
   (Pet, Tecnologia, Casa e Fitness), onde o Cupom Radar posta as ofertas.
2. O NICHO DA OFERTA É O CANAL DE ONDE ELA VEIO — não adivinha pelo título.
3. Para cada mensagem nova, extrai nome, preço original, preço atual, cupom e link.
4. Aplica os critérios definidos abaixo (CRITERIOS).
5. Se a oferta passar, adiciona no ofertas.json (com o campo "categoria")
   e sobe o arquivo para o repositório no GitHub.
6. As páginas do site leem o ofertas.json direto do GitHub e mostram só as
   ofertas do nicho de cada página.

ESTE ARQUIVO PRECISA FICAR RODANDO O TEMPO TODO EM UM SERVIDOR (Railway).
"""

import os
import re
import io
import json
import base64
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests
from PIL import Image, ImageOps, ImageDraw
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rei-de-copas-bot")

# ----------------------------------------------------------------------
# CONFIGURAÇÃO
# Tokens ficam SEMPRE nas variáveis de ambiente do Railway, nunca aqui.
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "seu-usuario/rei-de-copas-site")
GITHUB_FILE_PATH = os.environ.get("GITHUB_FILE_PATH", "ofertas.json")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

# Um canal privado por nicho. Os IDs abaixo são os padrões; se quiser trocar
# sem mexer no código, crie a variável de ambiente correspondente no Railway.
# (CHANNEL_ID, que já existe no Railway, continua valendo como canal do Pet.)
CANAIS_POR_NICHO = {
    "pet":        os.environ.get("CHANNEL_ID_PET") or os.environ.get("CHANNEL_ID") or "-1003969496525",
    "tecnologia": os.environ.get("CHANNEL_ID_TECNOLOGIA") or "-1004461839366",
    "casa":       os.environ.get("CHANNEL_ID_CASA") or "-1004478339694",
    "fitness":    os.environ.get("CHANNEL_ID_FITNESS") or "-1004401364148",
}
NICHO_POR_CANAL = {str(canal): nicho for nicho, canal in CANAIS_POR_NICHO.items() if canal}

# Critérios de aprovação — ajuste livremente.
CRITERIOS = {
    "desconto_min": 20,                 # em %
    "preco_min": 50,                    # em R$
    "preco_max": None,                  # None = sem limite superior
    "maximo_ofertas_por_nicho": 15,     # 3 destaques + 12 ofertas do dia, por nicho
    "validade_horas": 48,               # oferta mais velha que isso sai do site (use 99999 para desligar)
    "limite_diario_por_nicho": 15,      # teto de segurança de ofertas aprovadas por dia, por nicho
    "tolerancia_aumento_preco": 0.05,   # 5% — variações pequenas (centavos) não derrubam a oferta
    "intervalo_revalidacao_horas": 3,   # de quanto em quanto tempo o robô confere os preços já publicados
}

PALAVRAS_CHAVE_PET = [
    "pet", "pets", "cão", "cachorro", "gato", "gata", "felino", "canino",
    "ração", "petisco", "coleira", "guia", "caixa de areia",
    "brinquedo pet", "casinha", "cama pet", "tapete higiênico",
    "shampoo pet", "tosa", "veterinário", "focinheira", "comedouro", "bebedouro",
    "antipulgas", "vermífugo", "arranhador", "transportadora",
    "peitoral", "guia retrátil", "escova pet", "removedor de pelos",
]

# Filtro extra por palavra-chave. Só o Pet usa (mantém o comportamento de antes).
# Os outros nichos confiam no canal: None = sem filtro de palavra-chave.
PALAVRAS_CHAVE_POR_NICHO = {
    "pet": PALAVRAS_CHAVE_PET,
    "tecnologia": None,
    "casa": None,
    "fitness": None,
}

MARKETPLACES_CONFIAVEIS = [
    "amazon.com", "amzn.to",
    "mercadolivre.com", "meli.la", "mercadolibre.com",
    "shopee.com.br", "shope.ee",
]

ARQUIVO_CONTADOR = os.environ.get("GITHUB_CONTADOR_PATH", "contador_diario.json")

# CTA que acompanha cada oferta em destaque na landing page / redes sociais,
# convidando para o grupo de WhatsApp onde TODAS as ofertas são publicadas.
CTA_TEXTO = (
    "Quer receber essa e muitas outras ofertas em primeira mão? "
    "Entre no nosso grupo:"
)
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


PADRAO_LINHA_IGNORADA = r"^(de\s*R\$|R\$|cupom|https?://|loja oficial)"


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
        if l.strip() and not re.match(PADRAO_LINHA_IGNORADA, l.strip(), re.IGNORECASE)
    ]
    if linhas:
        resultado["nome"] = " — ".join(linhas[:2])

    return resultado


def avaliar(oferta, texto_original, nicho):
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

    palavras = PALAVRAS_CHAVE_POR_NICHO.get(nicho)
    if palavras:
        texto_lower = (texto_original or "").lower()
        if not any(palavra in texto_lower for palavra in palavras):
            motivos.append(f"nenhuma palavra-chave de {nicho} encontrada")
    return desconto, len(motivos) == 0, motivos

# ----------------------------------------------------------------------
# LIMITES POR NICHO (quantidade máxima e validade)
# ----------------------------------------------------------------------

def _data_captura(oferta):
    try:
        dt = datetime.fromisoformat(oferta["capturado_em"])
    except (KeyError, TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def aplicar_limites(ofertas, agora=None):
    """Recebe a lista com a oferta MAIS NOVA primeiro. Remove as vencidas
    (validade_horas) e mantém no máximo N por nicho. Ofertas antigas, sem o
    campo 'categoria', contam como Pet."""
    agora = agora or datetime.now(timezone.utc)
    validade = timedelta(hours=CRITERIOS["validade_horas"])
    por_nicho = Counter()
    resultado = []
    for oferta in ofertas:
        nicho = oferta.get("categoria") or "pet"
        dt = _data_captura(oferta)
        if dt is not None and agora - dt > validade:
            continue
        if por_nicho[nicho] >= CRITERIOS["maximo_ofertas_por_nicho"]:
            continue
        por_nicho[nicho] += 1
        resultado.append(oferta)
    return resultado

# ----------------------------------------------------------------------
# GITHUB — lê e atualiza o ofertas.json que as páginas consomem
# ----------------------------------------------------------------------

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
CONTADOR_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{ARQUIVO_CONTADOR}"


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


# Fundo creme e moldura na cor de destaque de cada nicho (mesma identidade das páginas).
COR_FUNDO_IMAGEM = (244, 241, 232)   # #F4F1E8
COR_MOLDURA_POR_NICHO = {
    "pet":        (214, 168, 79),    # #D6A84F dourado
    "tecnologia": (0, 194, 255),     # #00C2FF ciano
    "casa":       (193, 80, 46),     # #C1502E terracota
    "fitness":    (198, 255, 0),     # #C6FF00 verde-limão
}
TAMANHO_IMAGEM = 800
ESPESSURA_MOLDURA = 6
MARGEM_INTERNA = 46


def processar_imagem(bytes_originais, nicho="pet"):
    """Recebe a foto crua do Telegram e devolve uma versão quadrada,
    com fundo creme e moldura na cor do nicho, pronta para a página."""
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
        outline=COR_MOLDURA_POR_NICHO.get(nicho, COR_MOLDURA_POR_NICHO["pet"]),
        width=ESPESSURA_MOLDURA,
    )

    saida = io.BytesIO()
    canvas.save(saida, format="JPEG", quality=88)
    return saida.getvalue()


# ----------------------------------------------------------------------
# REVALIDAÇÃO DE PREÇO — confere se o preço anunciado ainda é real
# ----------------------------------------------------------------------
# Roda sozinha de tempos em tempos e tira do ar qualquer oferta cujo preço
# real, no link, esteja mais alto que o anunciado. NUNCA remove uma oferta
# só porque não conseguiu confirmar o preço — na dúvida, mantém como está.

HEADERS_REVALIDACAO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9",
}


def buscar_preco_atual(url, timeout=12):
    """Tenta confirmar o preço atual do produto no link de afiliado.
    Retorna um float ou None se não for possível confirmar com segurança —
    nunca chuta um valor."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers=HEADERS_REVALIDACAO, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return None
        pagina = resp.text
    except Exception as e:
        log.warning("Não foi possível abrir o link para revalidar preço (%s): %s", url, e)
        return None

    candidatos = []

    # Open Graph / schema.org — presente na maioria dos marketplaces sérios
    for padrao in [
        r'property="product:price:amount"\s+content="([\d.,]+)"',
        r'itemprop="price"\s+content="([\d.,]+)"',
    ]:
        m = re.search(padrao, pagina)
        if m:
            valor = numero_br(m.group(1)) if "," in m.group(1) else float(m.group(1))
            if valor:
                candidatos.append(valor)

    # Mercado Livre — preço fracionado (parte inteira + centavos)
    m = re.search(r'andes-money-amount__fraction">([\d.]+)<', pagina)
    if m:
        inteiro = m.group(1).replace(".", "")
        centavos_m = re.search(r'andes-money-amount__cents">(\d{1,2})<', pagina)
        centavos = centavos_m.group(1) if centavos_m else "00"
        candidatos.append(float(f"{inteiro}.{centavos}"))

    # Amazon — preço fracionado clássico
    m = re.search(r'a-price-whole">([\d.,]+)<', pagina)
    if m:
        inteiro = m.group(1).replace(".", "").replace(",", "")
        centavos_m = re.search(r'a-price-fraction">(\d{1,2})<', pagina)
        centavos = centavos_m.group(1) if centavos_m else "00"
        candidatos.append(float(f"{inteiro}.{centavos}"))

    if not candidatos:
        return None

    # usa o valor mais frequente entre os padrões encontrados
    return Counter(candidatos).most_common(1)[0][0]


async def revalidar_precos(context: ContextTypes.DEFAULT_TYPE):
    log.info("Revalidação periódica de preços iniciada...")
    try:
        ofertas, sha = carregar_ofertas_atuais()
    except Exception as e:
        log.error("Falha ao ler ofertas.json para revalidação: %s", e)
        return

    if not ofertas:
        return

    tolerancia = CRITERIOS["tolerancia_aumento_preco"]
    mantidas = []
    removidas = []
    mudou = False

    for oferta in ofertas:
        preco_listado = oferta.get("preco_atual")
        preco_real = buscar_preco_atual(oferta.get("link"))

        if preco_real is None or preco_listado is None:
            mantidas.append(oferta)  # não deu pra confirmar — não mexe
            continue

        if preco_real > preco_listado * (1 + tolerancia):
            removidas.append((oferta.get("nome"), preco_listado, preco_real))
            mudou = True
            continue

        if preco_real < preco_listado:
            oferta["preco_atual"] = preco_real  # preço caiu ainda mais — atualiza a favor do usuário
            mudou = True

        mantidas.append(oferta)

    if removidas:
        log.info("Removidas %s oferta(s) com preço desatualizado:", len(removidas))
        for nome, antigo, novo in removidas:
            log.info("   - %s | anunciado R$ %.2f | agora R$ %.2f", nome, antigo, novo)

    # tira também as ofertas vencidas (passaram da validade)
    dentro_da_validade = aplicar_limites(mantidas)
    if len(dentro_da_validade) != len(mantidas):
        log.info("Removidas %s oferta(s) vencidas ou acima do limite por nicho.",
                 len(mantidas) - len(dentro_da_validade))
        mudou = True
    mantidas = dentro_da_validade

    if mudou:
        try:
            salvar_ofertas(mantidas, sha)
            log.info("ofertas.json atualizado após revalidação de preços.")
        except Exception as e:
            log.error("Falha ao salvar ofertas.json após revalidação: %s", e)
    else:
        log.info("Revalidação concluída, nenhum preço fora do combinado.")


# ----------------------------------------------------------------------
# CONTADOR DIÁRIO (por nicho)
# ----------------------------------------------------------------------

def carregar_contador_diario():
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resp = requests.get(CONTADOR_API, headers=github_headers(), params={"ref": GITHUB_BRANCH})
    if resp.status_code == 404:
        return {"data": hoje, "contagem": {}}, None
    resp.raise_for_status()
    payload = resp.json()
    dados = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    # formato antigo (contagem era um número só) ou dia novo: recomeça
    if dados.get("data") != hoje or not isinstance(dados.get("contagem"), dict):
        dados = {"data": hoje, "contagem": {}}
    return dados, payload["sha"]


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

    # O nicho vem do canal. Mensagem de qualquer outro lugar é ignorada.
    nicho = NICHO_POR_CANAL.get(str(msg.chat_id))
    if not nicho:
        return

    texto = msg.text or msg.caption
    if not texto:
        return

    oferta = extrair_oferta(texto)
    desconto, aprovada, motivos = avaliar(oferta, texto, nicho)

    log.info(
        "[%s] Mensagem recebida | aprovada=%s | motivos=%s | texto=%.60s",
        nicho, aprovada, motivos, texto,
    )

    if not aprovada:
        return

    try:
        contador, sha_contador = carregar_contador_diario()
    except Exception as e:
        log.error("Falha ao ler contador diário no GitHub: %s", e)
        return

    if contador["contagem"].get(nicho, 0) >= CRITERIOS["limite_diario_por_nicho"]:
        log.info(
            "[%s] Limite diário de %s ofertas já atingido, ignorando.",
            nicho, CRITERIOS["limite_diario_por_nicho"],
        )
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
            bytes_processados = processar_imagem(bytes_originais, nicho)
            imagem_url = salvar_imagem(f"imagens/{id_oferta}.jpg", bytes_processados)
        except Exception as e:
            log.error("Falha ao baixar/processar/salvar imagem da oferta: %s", e)

    nova = {
        "id": id_oferta,
        "categoria": nicho,
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
    ofertas = aplicar_limites(ofertas)

    try:
        salvar_ofertas(ofertas, sha)
        log.info("[%s] Oferta aprovada e publicada: %s", nicho, nova["nome"])
        contador["contagem"][nicho] = contador["contagem"].get(nicho, 0) + 1
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

    intervalo_segundos = CRITERIOS["intervalo_revalidacao_horas"] * 60 * 60
    app.job_queue.run_repeating(
        revalidar_precos,
        interval=intervalo_segundos,
        first=10 * 60,  # espera 10 min após o robô subir antes da primeira checagem
        name="revalidar_precos",
    )

    for nicho, canal in CANAIS_POR_NICHO.items():
        log.info("Canal %-10s -> %s", nicho, canal)
    log.info("Robô no ar, escutando os canais do Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
