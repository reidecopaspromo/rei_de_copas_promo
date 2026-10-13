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
from datetime import datetime, timezone, timedelta, time as dtime

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
    "desconto_min": 20,                 # em % (produtos a partir de "faixa_barata_ate")
    "preco_min": 20,                    # em R$
    "faixa_barata_ate": 50,             # em R$: abaixo disso, vale o desconto mínimo mais alto
    "desconto_min_barato": 30,          # em % (produtos abaixo de "faixa_barata_ate")
    "preco_max": None,                  # None = sem limite superior
    "maximo_ofertas_por_nicho": 21,     # 3 destaques + 18 ofertas do dia, por nicho
    "validade_horas": 48,               # oferta mais velha que isso sai do site (use 99999 para desligar)
    "limite_diario_por_nicho": 40,      # teto de segurança de ofertas aprovadas por dia, por nicho
    "tolerancia_aumento_preco": 0.10,   # 10% (produtos a partir de "faixa_barata_ate"): até aqui o preço
                                        # é atualizado e a oferta continua no ar
    "tolerancia_aumento_preco_barato": 0.20,  # 20% (produtos abaixo de "faixa_barata_ate")
    "desconto_min_revalidacao": 10,     # em %: depois de publicada, só sai se o desconto cair abaixo disso
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
# Grupo de WhatsApp de cada nicho (o grupo geral virou painel interno)
LINK_GRUPO_POR_NICHO = {
    "pet":        "https://chat.whatsapp.com/KwAzQsQkH4CEHL3DuinUKL",
    "tecnologia": "https://chat.whatsapp.com/KZZWOsWCT1KFDYXINpJ6wj",
    "casa":       "https://chat.whatsapp.com/IGhdGWeevudHOUo6sifLNd",
    "fitness":    "https://chat.whatsapp.com/LxjPpTySPSK2MbpHGkZ6CC",
}

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


def desconto_exigido(preco):
    """Produtos baratos precisam de desconto maior para valer como oferta."""
    if (preco or 0) < CRITERIOS["faixa_barata_ate"]:
        return CRITERIOS["desconto_min_barato"]
    return CRITERIOS["desconto_min"]


def tolerancia_exigida(preco):
    """Quanto o preço pode subir sem derrubar a oferta. Produto barato oscila
    mais em termos percentuais, então tem folga maior."""
    if (preco or 0) < CRITERIOS["faixa_barata_ate"]:
        return CRITERIOS["tolerancia_aumento_preco_barato"]
    return CRITERIOS["tolerancia_aumento_preco"]


def avaliar(oferta, texto_original, nicho):
    if oferta["preco_original"] and oferta["preco_atual"]:
        desconto = round((oferta["preco_original"] - oferta["preco_atual"]) / oferta["preco_original"] * 100)
    else:
        desconto = 0
    motivos = []
    preco = oferta["preco_atual"] or 0
    exigido = desconto_exigido(preco)
    if desconto < exigido:
        motivos.append(f"desconto abaixo de {exigido}%")
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


def _cartoes_de_produto(no):
    """Percorre os dados da página e devolve todos os cartões de produto."""
    if isinstance(no, dict):
        cartoes = no.get("polycards")
        if isinstance(cartoes, list):
            for cartao in cartoes:
                if isinstance(cartao, dict):
                    yield cartao
        for valor in no.values():
            yield from _cartoes_de_produto(valor)
    elif isinstance(no, list):
        for valor in no:
            yield from _cartoes_de_produto(valor)


def preco_pagina_afiliado(pagina):
    """Os links meli.la abrem a página de afiliado do Mercado Livre
    (mercadolivre.com.br/social/...). Ela mostra o produto compartilhado em
    destaque e, abaixo, outras recomendações. O preço não está no HTML visível,
    e sim nos dados internos da página. Pega SÓ o preço do produto
    compartilhado, nunca o das recomendações. Retorna None se não achar."""
    marcador = "_n.ctx.r="
    inicio = pagina.find(marcador)
    if inicio == -1:
        return None
    try:
        dados, _ = json.JSONDecoder().raw_decode(pagina[inicio + len(marcador):])
        dados_pagina = dados["appProps"]["pageProps"]["data"]
        alvo = dados_pagina["shared_item"]["id"]
    except Exception:
        return None
    for cartao in _cartoes_de_produto(dados_pagina):
        meta = cartao.get("metadata") or {}
        if alvo not in (meta.get("id"), meta.get("product_id")):
            continue
        for componente in cartao.get("components") or []:
            if componente.get("type") != "price":
                continue
            valor = ((componente.get("price") or {}).get("current_price") or {}).get("value")
            if isinstance(valor, (int, float)) and valor > 0:
                return float(valor)
    return None


def buscar_preco_atual(url, timeout=12):
    """Tenta confirmar o preço atual do produto no link de afiliado.
    Retorna um float ou None se não for possível confirmar com segurança —
    nunca chuta um valor."""
    if not url:
        return None
    try:
        resp = requests.get(url, headers=HEADERS_REVALIDACAO, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            log.warning("Revalidação: link %s respondeu código %s (endereço final: %s)", url, resp.status_code, resp.url)
            return None
        pagina = resp.text
    except Exception as e:
        log.warning("Não foi possível abrir o link para revalidar preço (%s): %s", url, e)
        return None

    # Página de afiliado do Mercado Livre (onde os links meli.la caem)
    if "/social/" in resp.url:
        preco = preco_pagina_afiliado(pagina)
        if preco is None:
            log.warning("Revalidação: produto não encontrado na página de afiliado de %s", url)
        return preco

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
        log.warning("Revalidação: preço não encontrado na página de %s (endereço final: %s, tamanho %s)", url, resp.url, len(pagina))
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

    mantidas = []
    removidas = []
    atualizadas = []
    removidas_desconto = []
    conferidas = 0
    nao_conferidas = 0
    precos_lidos = []
    mudou = False

    for oferta in ofertas:
        preco_listado = oferta.get("preco_atual")
        preco_real = buscar_preco_atual(oferta.get("link"))

        if preco_real is None or preco_listado is None:
            nao_conferidas += 1
            mantidas.append(oferta)  # não deu pra confirmar — não mexe
            continue

        conferidas += 1
        precos_lidos.append((oferta, preco_real))

        # Subiu mais que a tolerância (20% abaixo de R$ 50, 10% a partir de R$ 50): sai do site
        if preco_real > preco_listado * (1 + tolerancia_exigida(preco_listado)):
            removidas.append((oferta.get("nome"), preco_listado, preco_real))
            mudou = True
            continue

        # Qualquer outra mudança (caiu, ou subiu até a tolerância): atualiza preço e desconto
        if abs(preco_real - preco_listado) >= 0.01:
            oferta["preco_atual"] = preco_real
            original = oferta.get("preco_original")
            if original:
                oferta["desconto"] = max(0, round((original - preco_real) / original * 100))
            atualizadas.append((oferta.get("nome"), preco_listado, preco_real))
            mudou = True

        # Se o preço não caiu e o desconto virou quase nada, sai do site.
        # Régua mais folgada que a da entrada (desconto_min_revalidacao), senão
        # qualquer alta pequena derrubaria a oferta e a tolerância não serviria.
        minimo_reval = CRITERIOS["desconto_min_revalidacao"]
        original = oferta.get("preco_original")
        if original and preco_real >= preco_listado - 0.01:
            desconto_agora = max(0, round((original - preco_real) / original * 100))
            if desconto_agora < minimo_reval:
                removidas_desconto.append((oferta.get("nome"), desconto_agora, minimo_reval))
                mudou = True
                continue

        mantidas.append(oferta)

    log.info("Revalidação: %s oferta(s) conferidas, %s não deu para conferir.",
             conferidas, nao_conferidas)
    if removidas_desconto:
        log.info("Revalidação: %s oferta(s) removidas por desconto abaixo do mínimo:", len(removidas_desconto))
        for nome, desc, exigido in removidas_desconto:
            log.info("   - %s | desconto agora %s%% | mínimo %s%%", nome, desc, exigido)
    if atualizadas:
        log.info("Revalidação: %s oferta(s) com preço atualizado:", len(atualizadas))
        for nome, antigo, novo in atualizadas:
            log.info("   - %s | era R$ %.2f | agora R$ %.2f", nome, antigo, novo)
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
        log.info("Revalidação concluída, nenhuma mudança de preço.")

    registrar_historico(precos_lidos)


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

def _chave_produto(oferta):
    """Nome do produto sem o trecho de preço (' — 😱 DE: R$...'), em minúsculas,
    para reconhecer o mesmo produto mesmo que o link de afiliado mude."""
    nome = (oferta.get("nome") or "").split(" — ")[0]
    return re.sub(r"\s+", " ", nome).strip().lower()


def _mesmo_produto(a, b):
    if a.get("link") and a.get("link") == b.get("link"):
        return True
    chave = _chave_produto(a)
    return bool(chave) and chave == _chave_produto(b)


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

    # Confere o preço na loja ANTES de publicar: a lista do Cupom Radar, quando
    # roda de novo, reenvia o preço da época em que o produto foi marcado.
    preco_loja = buscar_preco_atual(oferta["link"])
    anunciado = oferta["preco_atual"]
    if preco_loja is not None and anunciado:
        if preco_loja > anunciado * (1 + tolerancia_exigida(anunciado)):
            log.info("[%s] Oferta recusada, preço na loja subiu mais de %.0f%% "
                     "(mensagem R$ %.2f, loja R$ %.2f): %s",
                     nicho, tolerancia_exigida(anunciado) * 100,
                     anunciado, preco_loja, oferta["nome"])
            return
        if abs(preco_loja - anunciado) >= 0.01:
            log.info("[%s] Preço corrigido na entrada (mensagem R$ %.2f, loja R$ %.2f): %s",
                     nicho, anunciado, preco_loja, oferta["nome"])
            oferta["preco_atual"] = preco_loja
            desconto, aprovada, motivos = avaliar(oferta, texto, nicho)
            if not aprovada:
                log.info("[%s] Oferta recusada após corrigir o preço | motivos=%s", nicho, motivos)
                return
    elif preco_loja is None:
        log.info("[%s] Não deu para conferir o preço na loja, publicando com o preço da mensagem "
                 "(a próxima conferência corrige).", nicho)

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
        "link_grupo": LINK_GRUPO_POR_NICHO.get(nicho, LINK_GRUPO_WHATSAPP),
        "capturado_em": datetime.now(timezone.utc).isoformat(),
    }

    # Mesmo produto já no site: substitui pela versão nova (preço e validade atualizados)
    repetidas = [o for o in ofertas if _mesmo_produto(o, nova)]
    if repetidas:
        log.info("[%s] Produto já estava no site, substituindo pela versão nova: %s",
                 nicho, nova["nome"])
        ofertas = [o for o in ofertas if not _mesmo_produto(o, nova)]

    ofertas.insert(0, nova)
    ofertas = aplicar_limites(ofertas)

    try:
        salvar_ofertas(ofertas, sha)
        log.info("[%s] Oferta aprovada e publicada: %s", nicho, nova["nome"])
        contador["contagem"][nicho] = contador["contagem"].get(nicho, 0) + 1
        salvar_contador_diario(contador, sha_contador)
    except Exception as e:
        log.error("Falha ao salvar ofertas.json no GitHub: %s", e)


# ----------------------------------------------------------------------
# HISTÓRICO DE PREÇOS (base para os selos de "menor preço" na Black Friday)
# ----------------------------------------------------------------------
# A cada conferência, guarda o preço lido de cada produto em
# historico_precos.json (fora da pasta bot/, então gravar não reinicia o robô).
# Um registro por produto por dia (o mais recente do dia), até 120 dias.

ARQUIVO_HISTORICO = "historico_precos.json"
ARQUIVO_PAINEL = "painel.json"
ARQUIVO_ACHADOS = "achados.json"
DIAS_HISTORICO = 120
FUSO_BR = timezone(timedelta(hours=-3))


def _api_arquivo(caminho):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{caminho}"


def carregar_json_github(caminho, padrao):
    resp = requests.get(_api_arquivo(caminho), headers=github_headers(), params={"ref": GITHUB_BRANCH})
    if resp.status_code == 404:
        return padrao, None
    resp.raise_for_status()
    payload = resp.json()
    return json.loads(base64.b64decode(payload["content"]).decode("utf-8")), payload["sha"]


def salvar_json_github(caminho, dados, sha, mensagem):
    body = {
        "message": mensagem,
        "content": base64.b64encode(json.dumps(dados, ensure_ascii=False, indent=1).encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(_api_arquivo(caminho), headers=github_headers(), json=body)
    resp.raise_for_status()


def registrar_historico(precos_lidos):
    """precos_lidos: lista de (oferta, preco_real). Nunca interrompe a conferência."""
    if not precos_lidos:
        return
    hoje = datetime.now(FUSO_BR).strftime("%Y-%m-%d")
    limite = (datetime.now(FUSO_BR) - timedelta(days=DIAS_HISTORICO)).strftime("%Y-%m-%d")
    for tentativa in range(2):
        try:
            historico, sha = carregar_json_github(ARQUIVO_HISTORICO, {})
            for oferta, preco in precos_lidos:
                chave = _chave_produto(oferta)
                if not chave:
                    continue
                item = historico.setdefault(chave, {"nome": oferta.get("nome", "").split(" — ")[0], "precos": []})
                item["categoria"] = oferta.get("categoria") or "pet"
                precos = [p for p in item["precos"] if p[0] >= limite and p[0] != hoje]
                precos.append([hoje, round(float(preco), 2)])
                item["precos"] = precos
            salvar_json_github(ARQUIVO_HISTORICO, historico, sha, "Atualiza histórico de preços (robô Rei de Copas)")
            log.info("Histórico de preços atualizado (%s produtos lidos).", len(precos_lidos))
            return
        except requests.HTTPError as e:
            if tentativa == 0 and e.response is not None and e.response.status_code == 409:
                continue  # alguém gravou no meio do caminho: lê de novo e tenta mais uma vez
            log.warning("Não consegui salvar o histórico de preços: %s", e)
            return
        except Exception as e:
            log.warning("Não consegui salvar o histórico de preços: %s", e)
            return


# ----------------------------------------------------------------------
# ACHADO DO REI — imagem de story diária, uma por nicho, enviada ao painel
# ----------------------------------------------------------------------

ACHADO_HORARIOS = {          # horário de Brasília em que cada nicho é enviado ao painel
    "fitness": (7, 0),       # os 4 chegam juntos às 7h para agendar os stories do dia
    "pet": (7, 1),
    "casa": (7, 2),
    "tecnologia": (7, 3),
}
ACHADO_DIAS_SEM_REPETIR = 5  # o mesmo produto não volta a ser Achado antes disso
SITE = "https://reidecopaspromo.com.br"
PAGINA_DO_NICHO = {
    "pet": f"{SITE}/pet-promo-site.html",
    "tecnologia": f"{SITE}/tecnologia-promo-site.html",
    "casa": f"{SITE}/casa-promo-site.html",
    "fitness": f"{SITE}/fitness-promo-site.html",
}
TEMA_ACHADO = {
    # fundo, fundo2 (brilho), acento, creme, cor do texto em cima do acento
    "pet":        dict(nome="Pet",        fundo=(8, 42, 30),  fundo2=(11, 79, 60),  acento=(214, 168, 79), creme=(244, 241, 232), sobre_acento=(6, 61, 47)),
    "tecnologia": dict(nome="Tecnologia", fundo=(20, 27, 74), fundo2=(30, 42, 110), acento=(0, 194, 255),  creme=(238, 241, 251), sobre_acento=(20, 27, 74)),
    "casa":       dict(nome="Casa",       fundo=(62, 36, 21), fundo2=(104, 62, 34), acento=(193, 80, 46),  creme=(245, 237, 225), sobre_acento=(245, 237, 225)),
    "fitness":    dict(nome="Fitness",    fundo=(16, 20, 12), fundo2=(34, 46, 24),  acento=(198, 255, 0),  creme=(241, 243, 237), sobre_acento=(16, 20, 12)),
}
VERDE_MARCA = (6, 61, 47)
FONTES_URL = "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-{}.ttf"
_cache_fontes = {}
_cache_logo = {}


def _fonte(peso, tamanho):
    from PIL import ImageFont
    chave = (peso, tamanho)
    if chave in _cache_fontes:
        return _cache_fontes[chave]
    caminho = f"/tmp/rei_fontes/Montserrat-{peso}.ttf"
    try:
        if not os.path.exists(caminho):
            os.makedirs("/tmp/rei_fontes", exist_ok=True)
            r = requests.get(FONTES_URL.format(peso), timeout=20)
            r.raise_for_status()
            with open(caminho, "wb") as f:
                f.write(r.content)
        fonte = ImageFont.truetype(caminho, tamanho)
    except Exception as e:
        log.warning("Fonte Montserrat-%s indisponível (%s), usando a padrão.", peso, e)
        try:
            fonte = ImageFont.load_default(size=tamanho)
        except TypeError:
            fonte = ImageFont.load_default()
    _cache_fontes[chave] = fonte
    return fonte


def _logo():
    """Pega a logo do próprio site (fica na página Pet). Se não der, a imagem sai com o nome em texto."""
    if "img" in _cache_logo:
        return _cache_logo["img"]
    img = None
    try:
        pagina = requests.get(PAGINA_DO_NICHO["pet"], timeout=30).text
        m = re.search(r'class="logo-horizontal"[^>]*src="data:image/\w+;base64,([^"]+)"', pagina) or \
            re.search(r'<a[^>]*class="rh-logo"[^>]*>\s*<img src="data:image/\w+;base64,([^"]+)"', pagina)
        if m:
            img = Image.open(io.BytesIO(base64.b64decode(m.group(1)))).convert("RGBA")
    except Exception as e:
        log.warning("Não consegui pegar a logo no site: %s", e)
    _cache_logo["img"] = img
    return img


def _reais(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _quebrar(d, texto, fonte, largura, max_linhas):
    linhas, atual = [], ""
    for p in texto.split():
        teste = (atual + " " + p).strip()
        if d.textlength(teste, font=fonte) <= largura or not atual:
            atual = teste
        else:
            linhas.append(atual)
            atual = p
    linhas.append(atual)
    if len(linhas) > max_linhas:
        linhas = linhas[:max_linhas]
        while " " in linhas[-1] and d.textlength(linhas[-1] + "…", font=fonte) > largura:
            linhas[-1] = linhas[-1].rsplit(" ", 1)[0]
        linhas[-1] += "…"
    return linhas


def gerar_imagem_achado(oferta):
    """Monta o story 1080x1920 do Achado do Rei e devolve os bytes do JPG."""
    from PIL import ImageFilter
    W, H = 1080, 1920
    nicho = oferta.get("categoria") or "pet"
    t = TEMA_ACHADO.get(nicho, TEMA_ACHADO["pet"])

    def centro(d, y, txt, fonte, cor):
        d.text(((W - d.textlength(txt, font=fonte)) / 2, y), txt, font=fonte, fill=cor)

    img = Image.new("RGB", (W, H), t["fundo"])
    brilho = Image.new("L", (W // 4, H // 4), 0)
    ImageDraw.Draw(brilho).ellipse([30, 110, 240, 330], fill=255)
    brilho = brilho.filter(ImageFilter.GaussianBlur(40)).resize((W, H), Image.BILINEAR)
    img.paste(Image.new("RGB", (W, H), t["fundo2"]), (0, 0), brilho)
    d = ImageDraw.Draw(img)

    # faixa verde da marca com a logo
    d.rectangle([0, 0, W, 190], fill=VERDE_MARCA)
    logo = _logo()
    if logo is not None:
        lg = logo.copy()
        lg.thumbnail((420, 130))
        img.paste(lg, ((W - lg.width) // 2, (190 - lg.height) // 2), lg)
    else:
        centro(d, 62, "REI DE COPAS PROMO", _fonte("ExtraBold", 54), (214, 168, 79))

    # título com coroa
    s, cx, cy = 30, W // 2, 266
    pts = [(-1, .35), (-1, -.45), (-.5, -.05), (0, -.7), (.5, -.05), (1, -.45), (1, .35)]
    d.polygon([(cx + x * s, cy + y * s) for x, y in pts], fill=t["acento"])
    d.rounded_rectangle([cx - s, cy + .45 * s, cx + s, cy + .62 * s], radius=4, fill=t["acento"])
    centro(d, 300, "ACHADO DO REI", _fonte("Black", 70), t["creme"])
    centro(d, 392, f"O melhor de {t['nome']} hoje", _fonte("SemiBold", 34), t["acento"])

    # foto do produto (já vem com a moldura do nicho, feita pelo robô)
    lado = 680
    x0, y0 = (W - lado) // 2, 470
    foto = Image.open(io.BytesIO(requests.get(oferta["imagem"], timeout=20).content)).convert("RGB")
    sombra = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sombra).rounded_rectangle([x0 + 10, y0 + 24, x0 + lado + 10, y0 + lado + 24], 40, fill=150)
    img.paste((0, 0, 0), (0, 0), sombra.filter(ImageFilter.GaussianBlur(28)))
    mascara = Image.new("L", (lado, lado), 0)
    ImageDraw.Draw(mascara).rounded_rectangle([0, 0, lado, lado], 40, fill=255)
    img.paste(foto.resize((lado, lado), Image.LANCZOS), (x0, y0), mascara)
    d = ImageDraw.Draw(img)

    # selo de desconto
    if oferta.get("desconto"):
        cx, cy, r = x0 + lado - 24, y0 + 80, 100
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=t["acento"], outline=t["creme"], width=6)
        f = _fonte("Black", 58)
        txt = f"-{oferta['desconto']}%"
        d.text((cx - d.textlength(txt, font=f) / 2, cy - 46), txt, font=f, fill=t["sobre_acento"])
        f = _fonte("Bold", 26)
        d.text((cx - d.textlength("OFF", font=f) / 2, cy + 26), "OFF", font=f, fill=t["sobre_acento"])

    # nome
    f = _fonte("Bold", 44)
    y = y0 + lado + 46
    for linha in _quebrar(d, oferta.get("nome", "").split(" — ")[0].strip(), f, 900, 2):
        centro(d, y, linha, f, t["creme"])
        y += 58

    # preços
    y += 28
    cinza = tuple(int(c * .75) for c in t["creme"])
    if oferta.get("preco_original"):
        f = _fonte("Medium", 38)
        de = "De " + _reais(oferta["preco_original"])
        wd = d.textlength(de, font=f)
        xd = (W - wd) / 2
        d.text((xd, y), de, font=f, fill=cinza)
        d.line([xd, y + 26, xd + wd, y + 26], fill=cinza, width=3)
        y += 58
    f1, f2 = _fonte("Bold", 44), _fonte("Black", 104)
    a, b = "por ", _reais(oferta["preco_atual"])
    wa, wb = d.textlength(a, font=f1), d.textlength(b, font=f2)
    xs = (W - wa - wb) / 2
    d.text((xs, y + 50), a, font=f1, fill=t["creme"])
    d.text((xs + wa, y), b, font=f2, fill=(255, 255, 255))
    y += 145
    if oferta.get("preco_original") and oferta["preco_original"] > oferta["preco_atual"]:
        centro(d, y, "Economia de " + _reais(oferta["preco_original"] - oferta["preco_atual"]), _fonte("SemiBold", 34), t["acento"])

    # chamada
    y += 74
    f = _fonte("ExtraBold", 40)
    txt = "Toque no link"
    wt = d.textlength(txt, font=f)
    d.rounded_rectangle([(W - wt) / 2 - 60, y, (W + wt) / 2 + 60, y + 96], 48, fill=t["acento"])
    d.text(((W - wt) / 2, y + 24), txt, font=f, fill=t["sobre_acento"])

    # rodapé
    try:
        cap = datetime.fromisoformat(oferta["capturado_em"]).astimezone(FUSO_BR)
        quando = f"{cap:%d/%m} às {cap:%Hh%M}"
    except Exception:
        quando = datetime.now(FUSO_BR).strftime("%d/%m")
    f = _fonte("Regular", 25)
    centro(d, 1800, f"Preço conferido em {quando}. Pode mudar na loja.", f, cinza)
    centro(d, 1840, "Link de afiliado  |  reidecopaspromo.com.br", f, cinza)

    saida = io.BytesIO()
    img.save(saida, "JPEG", quality=90)
    return saida.getvalue()


def escolher_achado(nicho, recentes):
    """Maior desconto do nicho, com foto, que não foi Achado nos últimos dias."""
    ofertas, _ = carregar_ofertas_atuais()
    candidatas = [o for o in ofertas if (o.get("categoria") or "pet") == nicho and o.get("imagem") and o.get("preco_atual")]
    if not candidatas:
        return None
    candidatas.sort(key=lambda o: o.get("desconto") or 0, reverse=True)
    for o in candidatas:
        if _chave_produto(o) not in recentes:
            return o
    return candidatas[0]


async def enviar_achado(context, nicho, chat_id=None):
    """Gera e manda o Achado do Rei de um nicho para o painel."""
    try:
        painel, _ = carregar_json_github(ARQUIVO_PAINEL, {})
        chat_id = chat_id or painel.get("chat_id")
        if not chat_id:
            log.info("Achado do Rei (%s): painel ainda não conectado (mande /painel no canal).", nicho)
            return
        achados, sha = carregar_json_github(ARQUIVO_ACHADOS, {})
        limite = (datetime.now(FUSO_BR) - timedelta(days=ACHADO_DIAS_SEM_REPETIR)).strftime("%Y-%m-%d")
        recentes = {c for c, dia in achados.get(nicho, {}).items() if dia >= limite}
        oferta = escolher_achado(nicho, recentes)
        if not oferta:
            await context.bot.send_message(chat_id, f"👑 Achado do Rei ({TEMA_ACHADO[nicho]['nome']}): hoje não há ofertas no site para escolher.")
            return
        foto = gerar_imagem_achado(oferta)
        nome = oferta.get("nome", "").split(" — ")[0].strip()
        legenda = (
            f"👑 Achado do Rei | {TEMA_ACHADO[nicho]['nome']}\n"
            f"{nome}\n"
            + (f"De {_reais(oferta['preco_original'])} " if oferta.get("preco_original") else "")
            + f"por {_reais(oferta['preco_atual'])}"
            + (f" (-{oferta['desconto']}%)" if oferta.get("desconto") else "")
            + f"\n\n🔗 Adesivo de link do story:\n{PAGINA_DO_NICHO[nicho]}\n\n🛒 Oferta direta:\n{oferta.get('link', '')}"
        )
        await context.bot.send_photo(chat_id, photo=foto, caption=legenda[:1024])
        hoje = datetime.now(FUSO_BR).strftime("%Y-%m-%d")
        do_nicho = {c: dia for c, dia in achados.get(nicho, {}).items() if dia >= limite}
        do_nicho[_chave_produto(oferta)] = hoje
        achados[nicho] = do_nicho
        salvar_json_github(ARQUIVO_ACHADOS, achados, sha, "Registra Achado do Rei (robô Rei de Copas)")
        log.info("Achado do Rei (%s) enviado: %s", nicho, nome)
    except Exception as e:
        log.error("Falha ao enviar o Achado do Rei (%s): %s", nicho, e)


async def achado_agendado(context: ContextTypes.DEFAULT_TYPE):
    await enviar_achado(context, context.job.data)


# ----------------------------------------------------------------------
# PAINEL — canal privado do Telegram onde chegam os Achados
# ----------------------------------------------------------------------
# No canal: /painel conecta (só funciona se ainda não houver painel conectado)
#           /achado casa  -> gera o Achado do Rei de Casa na hora (teste)

async def comando_painel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    texto = msg.text.strip().lower()
    chat_id = msg.chat_id
    try:
        painel, sha = carregar_json_github(ARQUIVO_PAINEL, {})
    except Exception as e:
        log.error("Falha ao ler painel.json: %s", e)
        return

    if texto.startswith("/painel"):
        if painel.get("chat_id") and painel["chat_id"] != chat_id:
            log.warning("Pedido de /painel ignorado: já existe um painel conectado (%s).", painel["chat_id"])
            return
        salvar_json_github(ARQUIVO_PAINEL, {"chat_id": chat_id, "conectado_em": datetime.now(FUSO_BR).isoformat()},
                           sha, "Conecta painel do Telegram (robô Rei de Copas)")
        horarios = "\n".join(f"• {TEMA_ACHADO[n]['nome']}: {h:02d}h{m:02d}" for n, (h, m) in ACHADO_HORARIOS.items())
        await context.bot.send_message(
            chat_id,
            "✅ Painel do Rei conectado!\n\nO Achado do Rei chega aqui todo dia:\n" + horarios +
            "\n\nPara testar agora, mande: /achado casa (ou pet, tecnologia, fitness)",
        )
        log.info("Painel conectado: %s", chat_id)
        return

    if texto.startswith("/achado"):
        if painel.get("chat_id") != chat_id:
            return  # só responde no painel conectado
        partes = texto.split()
        nicho = partes[1] if len(partes) > 1 else ""
        if nicho not in TEMA_ACHADO:
            await context.bot.send_message(chat_id, "Use: /achado pet, /achado tecnologia, /achado casa ou /achado fitness")
            return
        await enviar_achado(context, nicho, chat_id)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Defina a variável de ambiente TELEGRAM_BOT_TOKEN antes de iniciar.")
    if not GITHUB_TOKEN:
        raise SystemExit("Defina a variável de ambiente GITHUB_TOKEN antes de iniciar.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & (~filters.COMMAND), nova_mensagem))
    app.add_handler(MessageHandler(
        (filters.UpdateType.MESSAGE | filters.UpdateType.CHANNEL_POST) & filters.Regex(r"(?i)^/(painel|achado)\b"),
        comando_painel,
    ))

    intervalo_segundos = CRITERIOS["intervalo_revalidacao_horas"] * 60 * 60
    app.job_queue.run_repeating(
        revalidar_precos,
        interval=intervalo_segundos,
        first=10 * 60,  # espera 10 min após o robô subir antes da primeira checagem
        name="revalidar_precos",
    )

    for nicho, (hora, minuto) in ACHADO_HORARIOS.items():
        app.job_queue.run_daily(achado_agendado, time=dtime(hora, minuto, tzinfo=FUSO_BR),
                                data=nicho, name=f"achado_{nicho}")

    for nicho, canal in CANAIS_POR_NICHO.items():
        log.info("Canal %-10s -> %s", nicho, canal)
    log.info("Robô no ar, escutando os canais do Telegram...")
    app.run_polling()


if __name__ == "__main__":
    main()
