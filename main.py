import os
import sys
import time
import json
import asyncio
import feedparser
import requests
import edge_tts
import xmlrpc.client
from urllib.parse import urlparse
from pydub import AudioSegment

# ==============================================================================
# CONFIGURAÇÕES (Variáveis de Ambiente / Secrets do GitHub)
# ==============================================================================
RAW_WP_URL = os.environ.get("WP_URL", "").strip()
WP_USER = os.environ.get("WP_USER", "").strip()
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.environ.get("GITHUB_REF_NAME", "main").strip()

# Garante a URL raiz do WordPress (remove subpastas como /podcasts caso tenham sido incluídas)
if RAW_WP_URL:
    parsed_wp = urlparse(RAW_WP_URL)
    WP_URL = f"{parsed_wp.scheme}://{parsed_wp.netloc}"
else:
    WP_URL = ""

VOICE_A = "pt-BR-AntonioNeural"    # Alex (Apresentador 1)
VOICE_B = "pt-BR-FranciscaNeural"  # Bia (Apresentadora 2)

# Feeds de notícias de IA e tecnologia em português
FEEDS_RSS = [
    "https://news.google.com/rss/search?q=inteligencia+artificial+when:2d&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "https://olhardigital.com.br/editorias/inteligencia-artificial/feed/",
    "https://canaltech.com.br/rss/",
    "https://rss.tecmundo.com.br/feed"
]

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ==============================================================================
# 1. BUSCA DE NOTÍCIAS E VERIFICAÇÃO DE DUPLICIDADE
# ==============================================================================
def obter_noticia_inedita():
    titulos_recentes = []
    try:
        r_blog = requests.get(f"{WP_URL}/feed/", headers=HEADERS_BROWSER, timeout=10)
        if r_blog.ok:
            blog_feed = feedparser.parse(r_blog.content)
            titulos_recentes = [e.title.lower() for e in blog_feed.entries[:15]]
            print(f"Títulos já existentes no blog: {len(titulos_recentes)}")
    except Exception as e:
        print(f"Aviso ao consultar feed do blog: {e}")

    for url_feed in FEEDS_RSS:
        try:
            print(f"Consultando feed: {url_feed}...")
            resp = requests.get(url_feed, headers=HEADERS_BROWSER, timeout=15)
            if not resp.ok:
                continue

            feed = feedparser.parse(resp.content)
            print(f"Notícias encontradas no feed: {len(feed.entries)}")

            for entry in feed.entries[:10]:
                titulo_limpo = entry.title.split(" - ")[0].strip()
                # Evita republicar tópicos já existentes
                if not any(titulo_limpo.lower() in t for t in titulos_recentes):
                    resumo = entry.summary if hasattr(entry, "summary") else titulo_limpo
                    return {
                        "titulo": titulo_limpo,
                        "link": entry.link,
                        "resumo_fonte": resumo
                    }
        except Exception as e:
            print(f"Erro ao processar feed {url_feed}: {e}")

    return None


# ==============================================================================
# 2. GERAÇÃO DO ROTEIRO (LLM com Retry e Fallback para 503)
# ==============================================================================
def gerar_roteiro(noticia: dict) -> dict:
    prompt = f"""
    Você é o roteirista de um mini podcast diário de 1 minuto sobre Inteligência Artificial chamado "Drops IA".
    Crie um bate-papo dinâmico e natural entre dois apresentadores: Alex e Bia.

    Notícia Base:
    Título: {noticia['titulo']}
    Contexto: {noticia['resumo_fonte']}

    Diretrizes Estritas:
    1. O diálogo total DEVE ter entre 130 e 145 palavras no total para durar exatamente ~55 segundos falado.
    2. Linguagem coloquial brasileira, fluida, informativa e ágil (estilo síntese do NotebookLM).
    3. Retorne APENAS um JSON válido no formato abaixo, sem tags markdown ou comentários:
    {{
      "titulo_episodio": "Título chamativo do episódio",
      "resumo_texto": "Resumo de 2 a 3 parágrafos explicando a notícia para os ouvintes lerem no post.",
      "dialogo": [
        {{"speaker": "Alex", "text": "fala do Alex..."}},
        {{"speaker": "Bia", "text": "fala da Bia..."}}
      ]
    }}
    """

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }

    modelos_fallback = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite"
    ]

    for modelo in modelos_fallback:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
        for tentativa in range(1, 4):
            print(f"Tentativa {tentativa} usando modelo: {modelo}...")
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=35)
                if resp.status_code == 200:
                    raw_json = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(raw_json)
                if resp.status_code in (503, 429):
                    print(f"Modelo {modelo} com pico de demanda (HTTP {resp.status_code}). Aguardando 5s...")
                    time.sleep(5)
                else:
                    print(f"Aviso ({resp.status_code}) em {modelo}: {resp.text}")
                    break
            except Exception as e:
                print(f"Falha de conexão com {modelo}: {e}")
                time.sleep(3)

    raise RuntimeError("Todos os modelos do Gemini falharam ou estão em alta demanda.")


# ==============================================================================
# 3. SÍNTESE DO ÁUDIO (Edge-TTS + Concatenação Pydub)
# ==============================================================================
async def sintetizar_dialogo(dialogo: list, arquivo_saida: str):
    temp_files = []
    for i, turno in enumerate(dialogo):
        temp_name = f"temp_parte_{i}.mp3"
        voz = VOICE_A if turno["speaker"] == "Alex" else VOICE_B
        comm = edge_tts.Communicate(turno["text"], voz)
        await comm.save(temp_name)
        temp_files.append(temp_name)

    audio_final = AudioSegment.empty()
    pausa = AudioSegment.silent(duration=200)

    for f in temp_files:
        audio_final += AudioSegment.from_file(f) + pausa
        if os.path.exists(f):
            os.remove(f)

    audio_final.export(arquivo_saida, format="mp3", bitrate="128k")
    return arquivo_saida


# ==============================================================================
# 4. PUBLICAÇÃO NO WORDPRESS.COM VIA XML-RPC
# ==============================================================================
def publicar_wordpress_com(dados_episodio: dict, audio_url: str):
    endpoint = f"{WP_URL}/xmlrpc.php"
    print(f"Conectando ao WordPress.com via XML-RPC em: {endpoint}...")
    server = xmlrpc.client.ServerProxy(endpoint)

    # Bloco Gutenberg nativo para o tema Spearhead com o link público da CDN
    bloco_audio = f"""<!-- wp:audio -->
<figure class="wp-block-audio"><audio controls src="{audio_url}"></audio></figure>
<!-- /wp:audio -->"""

    bloco_texto = f"""<!-- wp:heading {{"level":3}} -->
<h3 class="wp-block-heading">Notas do Episódio</h3>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>{dados_episodio['resumo_texto']}</p>
<!-- /wp:paragraph -->"""

    conteudo_completo = f"{bloco_audio}\n\n{bloco_texto}"

    post_data = {
        "post_title": f"Drops IA: {dados_episodio['titulo_episodio']}",
        "post_content": conteudo_completo,
        "post_status": "publish",
        "post_format": "audio",
        "terms_names": {
            "category": ["Podcasts", "Inteligência Artificial"],
            "post_tag": ["IA", "Tecnologia", "Drops IA"]
        }
    }

    post_id = server.wp.newPost(0, WP_USER, WP_APP_PASSWORD, post_data)
    return post_id


# ==============================================================================
# EXECUÇÃO PRINCIPAL
# ==============================================================================
def main():
    if not all([WP_URL, WP_USER, WP_APP_PASSWORD, GEMINI_API_KEY]):
        print("Erro: Variáveis de ambiente obrigatórias ausentes.")
        sys.exit(1)

    print(f"Site configurado: {WP_URL}")

    print("Etapa 1: Buscando notícias inéditas...")
    noticia = obter_noticia_inedita()
    if not noticia:
        print("Erro crítico: Nenhuma notícia pôde ser obtida dos feeds RSS.")
        sys.exit(1)

    print(f"Notícia selecionada: {noticia['titulo']}")

    print("Etapa 2: Gerando roteiro com IA...")
    dados = gerar_roteiro(noticia)

    print("Etapa 3: Sintetizando áudio com Edge-TTS...")
    os.makedirs("audios", exist_ok=True)
    nome_audio = f"episodio_{int(time.time())}.mp3"
    caminho_audio = os.path.join("audios", nome_audio)

    asyncio.run(sintetizar_dialogo(dados["dialogo"], caminho_audio))
    print(f"Áudio gerado com sucesso em: {caminho_audio}")

    # Monta a URL pública para o player (via CDN jsDelivr com o repositório do GitHub)
    audio_cdn_url = f"https://cdn.jsdelivr.net/gh/{GITHUB_REPOSITORY}@{GITHUB_REF_NAME}/audios/{nome_audio}"
    print(f"URL pública do áudio: {audio_cdn_url}")

    print("Etapa 4: Publicando no WordPress.com...")
    post_id = publicar_wordpress_com(dados, audio_cdn_url)
    print(f"Sucesso absoluto! Post #{post_id} publicado no WordPress.com!")


if __name__ == "__main__":
    main()
