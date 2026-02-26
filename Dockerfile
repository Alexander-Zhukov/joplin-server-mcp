FROM python:3.13-slim

RUN pip install --no-cache-dir mcp httpx uvicorn

WORKDIR /app
COPY app/server.py .

ENV JOPLIN_SERVER_URL=""
ENV JOPLIN_EMAIL=""
ENV JOPLIN_PASSWORD=""
ENV MCP_TRANSPORT="sse"
ENV MCP_HOST="0.0.0.0"
ENV MCP_PORT="8081"

EXPOSE 8081

CMD ["python", "server.py"]
