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
CTA_TEXTO = "Quer receber essa
