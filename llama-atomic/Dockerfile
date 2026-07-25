# Stage 1: compile llama-server (speculative decoding fork) for CC 8.6
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential curl libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
# Pinned to a commit SHA for reproducible/deterministic builds (the repo's
# default branch is master; there is NO 'main' branch). Override via
# --build-arg SOURCE_REF=<ref> or the ENGINE_SOURCE_REF repo var in CI.
ARG SOURCE_REF=a6f3d363e4c6a672803d0e6e036f8a75cf43d0ae
WORKDIR /src
RUN git init -q . \
    && git remote add origin https://github.com/AtomicBot-ai/atomic-llama-cpp-turboquant.git \
    && git fetch --depth 1 origin "$SOURCE_REF" \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1
# -DGGML_NATIVE=OFF: do NOT auto-detect the build runner's CPU instructions.
# CI runners have AVX512 but the target NUC (i3-8109U, Coffee Lake) does NOT ->
# an AVX512-enabled binary SIGILLs on load. Portable baseline + runtime dispatch.
RUN cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_NATIVE=OFF \
    && cmake --build build --config Release --target llama-server -j"$(nproc)"

# Stage 2: minimal runtime
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4 curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/build/bin/*.so* /usr/local/lib/
ENV LD_LIBRARY_PATH=/usr/local/lib
# Fix 16: build-time smoke check — fails the build if the binary or shared libs are broken
RUN llama-server --version 2>/dev/null || llama-server --help >/dev/null 2>&1
EXPOSE 8080
ENTRYPOINT []
CMD ["llama-server", "--host", "0.0.0.0", "--port", "8080"]
