FROM careerforge-api:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl clang llvm lld ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --retry 2 https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init -o /tmp/rustup-init && chmod +x /tmp/rustup-init && /tmp/rustup-init -y --profile minimal --default-toolchain 1.99.0 --no-modify-path && rm /tmp/rustup-init
ENV PATH="/root/.cargo/bin:$PATH"
RUN uv pip install --system cargo-xwin==0.23.1 && rustup target add x86_64-pc-windows-msvc
ENV XWIN_CACHE_DIR=/cache/xwin
WORKDIR /project/desktop/src-tauri
