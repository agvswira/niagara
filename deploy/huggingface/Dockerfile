# Hugging Face Space for the Niagara live demo.
# The Space holds only this file and README.md; the code is cloned from the public GitHub repo at build time.
# After pushing to GitHub, use "Factory rebuild" in the Space settings to pick up the new commit.
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends git fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000, so the app directory must belong to that user for SQLite and uploads
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PORT=7860 \
    LLM_CHAIN=compat,gemini \
    OPENAI_COMPAT_MODEL=openai/gpt-oss-120b \
    OPENAI_COMPAT_REASONING_EFFORT=low \
    GEMINI_MODEL=gemini-3.6-flash \
    COMPANY_NAME="CV Maju Mundur Sejahtera"

WORKDIR /home/user
RUN git clone --depth 1 https://github.com/agvswira/niagara.git app
WORKDIR /home/user/app
RUN pip install --no-cache-dir --user -r requirements.txt

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
