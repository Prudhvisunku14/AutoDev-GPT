# devpilot/document-parser:1: where untrusted Google Drive documents are parsed (SECURITY.md §8; app/rag/parsers.py).
# SPECIFICATION ONLY: never built, never run, not approved. Build context: backend/
#   docker build -f sandbox_images/document-parser/Dockerfile -t devpilot/document-parser:1 .
#
# Standard library only: TXT, Google Docs (exported text) and DOCX. PDF is NOT supported until an approved PDF parser
# is chosen, pinned by hash and added here, with approval. Base: python:3.12-alpine, the image the sandbox already
# approves; pin it by image ID at approval. Every job runs with the sandbox's flags: no network, all capabilities
# dropped, read-only root and workspace, user 65534, and memory, CPU, process and time limits.
FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1
COPY app/rag/parsing.py /opt/parser/parsing.py
COPY sandbox_images/document-parser/parse_main.py /opt/parser/parse_main.py

USER 65534:65534
WORKDIR /workspace
