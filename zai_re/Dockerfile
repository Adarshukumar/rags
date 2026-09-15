# zai_re replay server — no third-party dependencies, stdlib only.
#
#   docker build -f zai_re/Dockerfile -t zai-re .
#   docker run --rm -p 8801:8801 zai-re
#   # then open http://localhost:8801/  (demo UI)  or GET /health
#
# NOTE: docker was not available in the environment where this was developed,
# so the image is provided untested; the module itself runs with plain Python:
#   python3 -m zai_re.mock_server --port 8801
FROM python:3.11-slim

WORKDIR /app

# only what the replay server needs: the package + the captured HAR
COPY zai_re/ /app/zai_re/
COPY chat.z.ai.har /app/chat.z.ai.har

ENV PYTHONUNBUFFERED=1 \
    ZAI_PORT=8801

EXPOSE 8801

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8801/health',timeout=2).status==200 else 1)"

CMD ["python3", "-m", "zai_re.mock_server", "--port", "8801", "--host", "0.0.0.0"]
