import json
from collections import defaultdict

# 1. Lendo o arquivo categorias.json
with open("categorias.json", "r", encoding="utf-8") as f:
    categorias_brutas = json.load(f)

# 2. Criando nosso "armário de gavetas" inteligente
# Dizemos que o padrão (default) para cada nova gaveta é ser uma lista vazia (list)
grupos_de_nichos = defaultdict(list)

# 3. O Loop de Agrupamento
for categoria in categorias_brutas:
    # A função split('_') quebra o texto numa lista de palavras: ["3D", "PRINTERS"]
    # O [0] pega o primeiro item dessa lista (o prefixo)
    prefixo = categoria.split('_')[0]
    
    # Adicionamos a categoria inteira na lista do prefixo correspondente
    grupos_de_nichos[prefixo].append(categoria)

# 4. Vendo o resultado de forma bonita
for prefixo, lista_de_categorias in grupos_de_nichos.items():
    print(f"\nNicho (Prefixo): {prefixo}")
    print(f"Total de itens: {len(lista_de_categorias)}")
    print(f"Categorias: {lista_de_categorias}")