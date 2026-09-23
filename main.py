import os
import sys
import json
import base64
import asyncio
import feedparser
import requests
import edge_tts
from pydub import AudioSegment

# ==============================================================================
# CONFIGURAÇÕES (Variáveis de Ambiente / Secrets)
# ==============================================================================
WP_URL = os.environ.get("WP_URL", "").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Configuração de Vozes Edge-TTS (Português do Brasil)
VOICE_A = "pt-BR-AntonioNeural"    # Apresentador 1
VOICE_B = "pt-BR-FranciscaNeural"  # Apresentadora 2

RSS_FEED_URL = "https://news.google.com/rss/search?q=inteligencia+artificial+when:1d&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def get_wp_auth_header():
    credenciais = f"{WP_USER}:{WP_APP_PASSWORD}"
    token = base64.b64encode(credenciais.encode()).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


# ==============================================================================
# 1. BUSCA DE NOTÍCIAS E VERIFICAÇÃO DE DUPLICIDADE
# ==============================================================================
def obter_noticia_inedita():
    """Busca notícia do RSS e evita republicar títulos já postados recentemente."""
    feed = feedparser.parse(RSS_FEED_URL)
    if not feed.entries:
        print("Nenhuma notícia encontrada no feed RSS.")
        return None

    # Consulta os últimos 10 posts do WordPress para comparar títulos
    titulos_recentes = []
    try:
        resp = requests.get(f"{WP_URL}/wp-json/wp/v2/posts?per_page=10", headers=get_wp_auth_header(), timeout=15)
        if resp.status_code == 200:
            titulos_recentes = [p["title"]["rendered"].lower() for p in resp.json()]
    except Exception as e:
        print(f"Aviso ao consultar posts recentes: {e}")

    for entry in feed.entries[:8]:
        titulo_limpo = entry.title.split(" - ")[0].strip()
        # Evita duplicatas simples
        if not any(titulo_limpo.lower() in t for t in titulos_recentes):
            return {
                "titulo": titulo_limpo,
                "link": entry.link,
                "resumo_fonte": entry.summary if hasattr(entry, "summary") else titulo_limpo
            }

    print("Todas as notícias recentes do feed já foram publicadas.")
    return None


# ==============================================================================
# 2. GERAÇÃO DO ROTEIRO (LLM - Roteiro de 1 min / 130-145 palavras)
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

  headers = {"Content-Type": "application/json"}
  payload = {
      "contents": [{"parts": [{"text": prompt}]}],
      "generationConfig": {"response_mime_type": "application/json"},
  }

  # Endpoint com o modelo oficial gemini-1.5-flash
  url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"

  resp = requests.post(url, json=payload, headers=headers, timeout=30)
  resp.raise_for_status()
  raw_json = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
  return json.loads(raw_json)

# ==============================================================================
# 3. SÍNTESE DO ÁUDIO (Edge-TTS + Concatenação Pydub)
# ==============================================================================
async def sintetizar_dialogo(dialogo: list, arquivo_saida="episodio.mp3"):
    temp_files = []
    for i, turno in enumerate(dialogo):
        temp_name = f"temp_parte_{i}.mp3"
        voz = VOICE_A if turno["speaker"] == "Alex" else VOICE_B
        comm = edge_tts.Communicate(turno["text"], voz)
        await comm.save(temp_name)
        temp_files.append(temp_name)

    audio_final = AudioSegment.empty()
    pausa = AudioSegment.silent(duration=200)  # 200ms de respiro entre falas

    for f in temp_files:
        audio_final += AudioSegment.from_file(f) + pausa
        if os.path.exists(f):
            os.remove(f)

    audio_final.export(arquivo_saida, format="mp3", bitrate="128k")
    return arquivo_saida


# ==============================================================================
# 4. PUBLICAÇÃO NO WORDPRESS (Compatível com Tema Spearhead)
# ==============================================================================
def publicar_wordpress(dados_episodio: dict, caminho_audio: str):
    headers_auth = get_wp_auth_header()

    # 4.1. Upload do Arquivo de Áudio
    nome_arquivo = f"drops_ia_{os.path.basename(caminho_audio)}"
    with open(caminho_audio, "rb") as f:
        media_headers = {
            **headers_auth,
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "Content-Type": "audio/mpeg"
        }
        media_resp = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers=media_headers,
            data=f,
            timeout=60
        )
        media_resp.raise_for_status()
        media_data = media_resp.json()
        audio_id = media_data["id"]
        audio_url = media_data["source_url"]

    # 4.2. Estruturação do Conteúdo com o Bloco Nativo de Áudio do Gutenberg
    bloco_audio = f"""<!-- wp:audio {{"id":{audio_id}}} -->
<figure class="wp-block-audio"><audio controls src="{audio_url}"></audio></figure>
<!-- /wp:audio -->"""

    bloco_texto = f"""<!-- wp:heading {{"level":3}} -->
<h3 class="wp-block-heading">Notas do Episódio</h3>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>{dados_episodio['resumo_texto']}</p>
<!-- /wp:paragraph -->"""

    conteudo_post = f"{bloco_audio}\n\n{bloco_texto}"

    post_payload = {
        "title": f"Drops IA: {dados_episodio['titulo_episodio']}",
        "content": conteudo_post,
        "status": "publish",  # Pode trocar por 'draft' se preferir revisar antes
        "format": "audio"     # Formato de post 'audio' suportado pelo Spearhead
    }

    post_headers = {**headers_auth, "Content-Type": "application/json"}
    post_resp = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        headers=post_headers,
        json=post_payload,
        timeout=30
    )
    post_resp.raise_for_status()
    return post_resp.json()


# ==============================================================================
# EXECUÇÃO PRINCIPAL
# ==============================================================================
def main():
    if not all([WP_URL, WP_USER, WP_APP_PASSWORD, GEMINI_API_KEY]):
        print("Erro: Verifique se todas as variáveis de ambiente foram configuradas.")
        sys.exit(1)

    print("Etapa 1: Buscando notícias...")
    noticia = obter_noticia_inedita()
    if not noticia:
        print("Encerrando execução sem novas publicações.")
        return

    print(f"Notícia selecionada: {noticia['titulo']}")

    print("Etapa 2: Gerando roteiro calibrado para 1 minuto...")
    dados = gerar_roteiro(noticia)

    print("Etapa 3: Sintetizando vozes neurais (Alex e Bia)...")
    asyncio.run(sintetizar_dialogo(dados["dialogo"], "episodio.mp3"))

    print("Etapa 4: Publicando no WordPress (Tema Spearhead)...")
    post = publicar_wordpress(dados, "episodio.mp3")
    print(f"Sucesso! Episódio publicado em: {post.get('link')}")

    if os.path.exists("episodio.mp3"):
        os.remove("episodio.mp3")


if __name__ == "__main__":
    main()