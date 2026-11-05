# KaigoShiftVoice の公開版（Hugging Face Spaces の Docker）。手元で試す時はリポジトリの頭で
#   docker build -f deploy/hf-space/Dockerfile -t shiftvoice-public .
#   docker run --rm -p 7860:7860 -e ANTHROPIC_API_KEY shiftvoice-public
# 声の部品（sherpa-onnx・声のモデル）は入れない。鍵はここに書かず、Spaces の Secrets から実行の時に受け取る
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/home/user/app/src \
    SHIFTVOICE_PUBLIC=1 \
    PORT=7860

RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# 依存は pyproject.toml の dependencies（版を固定した anthropic と ortools）だけを入れる
COPY --chown=user pyproject.toml LICENSE ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && rm /tmp/req.txt

COPY --chown=user src ./src
COPY --chown=user data/hidamari ./data/hidamari

USER user
EXPOSE 7860
CMD ["python", "-c", "import shiftvoice; shiftvoice.main()", "demo", "--no-open"]
