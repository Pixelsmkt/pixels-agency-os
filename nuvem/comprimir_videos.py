#!/usr/bin/env python3
"""
Compressão de vídeo NA NUVEM (GitHub Actions) — 30/09/2026.

Faz exatamente o mesmo trabalho do previews_ffmpeg.py do PC da agência, sem depender
de nenhum computador ligado:
  1. pede ao Supabase a lista de vídeos sem versão leve (rpc pixels_preview_candidates);
  2. "reserva" cada vídeo na tabela preview_backfill (claimed_by = "nuvem"), pra dois
     robôs não comprimirem o mesmo vídeo ao mesmo tempo;
  3. baixa o original, gera a versão leve 720p com ffmpeg e sobe ao lado do original
     como "<nome>-preview.mp4";
  4. grava no card (rpc pixels_set_file_preview, previewEngine = "ffmpeg").

Não apaga nada: o original fica intacto, e o PC da agência continua podendo rodar como
reserva. Repositório é PÚBLICO: o log NÃO mostra nomes de arquivo, cliente ou card.

Segredos (GitHub → Settings → Secrets and variables → Actions):
  SUPABASE_URL          https://<projeto>.supabase.co
  SUPABASE_SERVICE_KEY  chave service_role (Supabase → Project Settings → API)
"""
import os, sys, time, json, hashlib, subprocess, tempfile, shutil
from datetime import datetime, timezone, timedelta
import requests

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BUCKET = "agency-files"
TEMPO_MAX = int(os.environ.get("TEMPO_MAX_SEG", str(40 * 60)))  # sai antes do timeout do job
RESERVA_VALIDA = timedelta(minutes=20)   # mesma janela do pixels_claim_preview
ESPERA_FALHA = timedelta(hours=2)        # vídeo que falhou só é tentado de novo depois disso
ROBO = "nuvem"

H = {"apikey": KEY, "Authorization": "Bearer " + KEY}
S = requests.Session()
S.headers.update(H)


def log(msg):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), msg, flush=True)


def sid(path):
    """Identificador curto e anônimo pro log (repo público)."""
    return hashlib.sha1(path.encode()).hexdigest()[:8]


def rpc(nome, **args):
    r = S.post(f"{URL}/rest/v1/rpc/{nome}", json=args, timeout=60)
    r.raise_for_status()
    return r.json() if r.text else None


def agora():
    return datetime.now(timezone.utc)


def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def reservar(path, task_id, ja_tem_leve):
    """True se a nuvem ficou com o vídeo. Não mexe em vídeo 'done', nem em reserva
    recente de outro robô/navegador, nem em falha recente."""
    r = S.get(f"{URL}/rest/v1/preview_backfill",
              params={"storage_path": "eq." + path, "select": "status,claimed_by,claimed_at,updated_at"},
              timeout=30)
    r.raise_for_status()
    rows = r.json()
    if rows:
        b = rows[0]
        st = b.get("status")
        if st == "done" and ja_tem_leve:
            return False  # o navegador já fez uma versão leve; não gasta de novo
        if st == "processing" and b.get("claimed_by") != ROBO:
            c = _dt(b.get("claimed_at"))
            if c and agora() - c < RESERVA_VALIDA:
                return False
        if st == "failed":
            u = _dt(b.get("updated_at"))
            if u and agora() - u < ESPERA_FALHA:
                return False
    agora_iso = agora().isoformat()
    body = {"storage_path": path, "task_id": str(task_id), "status": "processing",
            "claimed_by": ROBO, "claimed_at": agora_iso, "updated_at": agora_iso}
    r = S.post(f"{URL}/rest/v1/preview_backfill",
               params={"on_conflict": "storage_path"},
               headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
               json=body, timeout=30)
    r.raise_for_status()
    return True


def finalizar(path, ok):
    body = {"status": "done" if ok else "failed", "updated_at": agora().isoformat()}
    if ok:
        body["done_at"] = body["updated_at"]
    try:
        S.patch(f"{URL}/rest/v1/preview_backfill", params={"storage_path": "eq." + path},
                headers={"Prefer": "return=minimal"}, json=body, timeout=30).raise_for_status()
    except Exception as e:
        log(f"  aviso: não consegui marcar a fila ({type(e).__name__})")


def baixar(path, destino):
    with S.get(f"{URL}/storage/v1/object/{BUCKET}/{path}", stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        with open(destino, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)


def duracao(arq):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", arq], capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


def tem_audio(arq):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", arq], capture_output=True, text=True, timeout=60)
    return bool(out.stdout.strip())


def comprimir(orig, saida):
    """720p (maior lado 1280), H.264 + AAC, faststart (abre rápido no navegador).
    Bitrate adaptativo igual ao do app: mira ~13 MB, piso 1.2 Mbps, teto 6 Mbps."""
    d = duracao(orig) or 60.0
    alvo_bps = int(13 * 1024 * 1024 * 8 / d) - 128000
    vbps = max(1_200_000, min(6_000_000, alvo_bps))
    escala = "scale='if(gte(iw,ih),min(1280,iw),-2)':'if(gte(iw,ih),-2,min(1280,ih))',format=yuv420p"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", orig,
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", escala,
           "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
           "-b:v", str(vbps), "-maxrate", str(int(vbps * 1.5)), "-bufsize", str(vbps * 2),
           "-c:a", "aac", "-b:a", "128k", "-ac", "2",
           "-movflags", "+faststart", saida]
    if not tem_audio(orig):
        cmd = [c for c in cmd if c not in ("-c:a", "aac", "-b:a", "128k", "-ac", "2")]
        cmd.insert(cmd.index("-vf"), "-an")
    subprocess.run(cmd, check=True, timeout=45 * 60)


def subir(path_preview, arq):
    with open(arq, "rb") as f:
        r = S.post(f"{URL}/storage/v1/object/{BUCKET}/{path_preview}",
                   headers={"Content-Type": "video/mp4", "x-upsert": "true", "cache-control": "3600"},
                   data=f, timeout=(30, 900))
    r.raise_for_status()
    return f"{URL}/storage/v1/object/public/{BUCKET}/{path_preview}"


def processar(c, tmp):
    path, task_id, size = c["path"], c["task_id"], int(c.get("size") or 0)
    tag = sid(path)
    if not reservar(path, task_id, bool(c.get("preview_path"))):
        log(f"[{tag}] já está com outro robô ou pronto — pulando")
        return
    ok = False
    orig = os.path.join(tmp, "orig" + os.path.splitext(path)[1])
    out = os.path.join(tmp, "leve.mp4")
    try:
        t0 = time.time()
        baixar(path, orig)
        comprimir(orig, out)
        novo = os.path.getsize(out)
        real = os.path.getsize(orig)
        if novo >= real * 0.9:
            # não encolheu de verdade — não vale subir; o full já serve
            log(f"[{tag}] versão leve não ficou menor ({novo//1048576} MB) — descartada")
        else:
            base = path.rsplit(".", 1)[0]
            ppath = base + "-preview.mp4"
            url = subir(ppath, out)
            ok = bool(rpc("pixels_set_file_preview", p_task_id=str(task_id), p_storage_path=path,
                          p_url=url, p_path=ppath, p_size=novo))
            log(f"[{tag}] {real//1048576} MB → {novo//1048576} MB em {int(time.time()-t0)}s "
                + ("✓" if ok else "(card não encontrado)"))
    except Exception as e:
        log(f"[{tag}] erro: {type(e).__name__}")
    finally:
        finalizar(path, ok)
        for a in (orig, out):
            try:
                os.remove(a)
            except OSError:
                pass


def main():
    if not URL or not KEY:
        log("Faltam os segredos SUPABASE_URL / SUPABASE_SERVICE_KEY no GitHub.")
        sys.exit(1)
    inicio = time.time()
    feitos_nesta = set()
    tmp = tempfile.mkdtemp(prefix="px-")
    try:
        while time.time() - inicio < TEMPO_MAX:
            cands = rpc("pixels_preview_candidates", p_limit=20) or []
            cands = [c for c in cands if c.get("path") and c["path"] not in feitos_nesta]
            if not cands:
                break
            log(f"{len(cands)} vídeo(s) sem versão comprimida")
            for c in cands:
                if time.time() - inicio >= TEMPO_MAX:
                    break
                feitos_nesta.add(c["path"])
                processar(c, tmp)
        log("fim da passada")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
