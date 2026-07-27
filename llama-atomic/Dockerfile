# Stage 1: compile llama-server (speculative decoding fork) for CC 8.6
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential curl libcurl4-openssl-dev python3 \
    && rm -rf /var/lib/apt/lists/*
# CUDA driver stub for the link step (libggml-cuda.so needs libcuda.so.1).
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1
ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
# Pinned to a commit SHA for reproducible/deterministic builds (the repo's
# default branch is master; there is NO 'main' branch). Override via
# --build-arg SOURCE_REF=<ref> or the ENGINE_SOURCE_REF repo var in CI.
ARG SOURCE_REF=a6f3d363e4c6a672803d0e6e036f8a75cf43d0ae
COPY ui.stub.h /tmp/ui.stub.h
COPY extract_flags.py /tmp/extract_flags.py
WORKDIR /src
RUN git init -q . \
    && git remote add origin https://github.com/AtomicBot-ai/atomic-llama-cpp-turboquant.git \
    && git fetch --depth 1 origin "$SOURCE_REF" \
    && git checkout -q FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && sed -i 's/ llama-ui / /' tools/server/CMakeLists.txt \
    && cp /tmp/ui.stub.h tools/server/ui.h
# -DGGML_NATIVE=OFF: do NOT auto-detect the build runner's CPU instructions.
# CI runners have AVX512 but the target NUC (i3-8109U, Coffee Lake) does NOT ->
# an AVX512-enabled binary SIGILLs on load. Portable baseline + runtime dispatch.
# API-only: drop the embedded Web UI (-DLLAMA_BUILD_UI=OFF source guard + sed to
# remove the unconditional 'llama-ui' link in tools/server/CMakeLists.txt).
RUN cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DGGML_NATIVE=OFF -DLLAMA_BUILD_UI=OFF \
    -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs \
    && cmake --build build --config Release --target llama-server -j"$(nproc)" \
    && LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs /src/build/bin/llama-server --help 2>/dev/null | python3 /tmp/extract_flags.py /etc/llama-engine/flags.toml

# Stage 2: minimal runtime
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4 curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/build/bin/*.so* /usr/local/lib/
COPY --from=builder /etc/llama-engine/flags.toml /etc/llama-engine/flags.toml
ENV LD_LIBRARY_PATH=/usr/local/lib
# Build-time flag schema extraction uses libcuda.so stub to run llama-server --help without GPU.
EXPOSE 8080
ENTRYPOINT []
CMD ["llama-server", "--host", "0.0.0.0", "--port", "8080"]
