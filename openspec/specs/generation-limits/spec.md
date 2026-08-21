# generation-limits Specification

## Purpose

Injects a server-side default `max_tokens` into forwarded chat completion requests when a client omits any generation-length bound, preventing a model with unreliable EOS emission (e.g. heretic fine-tunes) from generating indefinitely and holding the GPU/stream open.

## Requirements

### Requirement: Default Injection When Client Omits a Generation Bound

The wrapper MUST inject a resolved default generation-length value into the request body forwarded to the engine when the client's request contains none of `max_tokens`, `max_completion_tokens`, or `n_predict`. An explicit `null` value for any of these three keys MUST be treated as omitted (eligible for injection). This applies to BOTH the non-streaming path (`chat_completions()`) and the streaming path (`stream_upstream()`).

#### Scenario: Client omits all three keys — default injected

- GIVEN a `/v1/chat/completions` request body with no `max_tokens`, `max_completion_tokens`, or `n_predict` key
- WHEN the wrapper forwards the request to the engine (non-streaming)
- THEN the forwarded body contains the resolved default value under the appropriate key

#### Scenario: Client sends explicit null — treated as omitted

- GIVEN a request body with `"max_tokens": null`
- WHEN the wrapper processes the request
- THEN the wrapper injects the resolved default exactly as if the key were absent

#### Scenario: Streaming request also receives injection

- GIVEN a request body with `"stream": true` and no generation-length key set
- WHEN `stream_upstream()` forwards the request
- THEN the forwarded body contains the resolved default value, identically to the non-streaming path

### Requirement: Injection Never Overwrites an Explicit Client Value

If the client supplies any non-null value for `max_tokens`, `max_completion_tokens`, or `n_predict`, the wrapper MUST forward the request unmodified with respect to generation-length fields.

#### Scenario: Explicit client value is preserved

- GIVEN a request body with `"max_tokens": 256`
- WHEN the wrapper forwards the request
- THEN the forwarded body still contains `"max_tokens": 256`, unchanged

### Requirement: Precedence Order for the Resolved Default

The resolved default value MUST be determined by this precedence, highest first: (1) the per-model `[args].default_max_tokens` config key, (2) the `DEFAULT_MAX_TOKENS` environment variable (default value **4096**), (3) no injection at all if the resolved value is `0`.

#### Scenario: Per-model override takes precedence over env default

- GIVEN a model config with `[args].default_max_tokens = 1024` and `DEFAULT_MAX_TOKENS=4096` in the environment
- WHEN a request for that model omits a generation-length key
- THEN the injected value is `1024`, not `4096`

#### Scenario: Env default applies when no per-model override exists

- GIVEN a model config with no `default_max_tokens` key and `DEFAULT_MAX_TOKENS=4096`
- WHEN a request for that model omits a generation-length key
- THEN the injected value is `4096`

#### Scenario: Resolved value of zero disables injection

- GIVEN a model config with `[args].default_max_tokens = 0` (or `DEFAULT_MAX_TOKENS=0` with no per-model override)
- WHEN a request for that model omits a generation-length key
- THEN no generation-length key is injected AND the request is forwarded exactly as received

### Requirement: Injection Is Logged

Whenever the wrapper injects a default generation-length value, it MUST log an INFO-level line naming the model alias and the resolved injected value.

#### Scenario: Injection produces a traceable log line

- GIVEN a request without a generation-length key resolves to an injected default of `4096` for alias `gemma-4-12b-it-heretic`
- WHEN the wrapper injects the value
- THEN an INFO log line is emitted containing both the alias and `4096`, so a truncated-looking answer is traceable to the default rather than a model failure

### Requirement: default_max_tokens Is Allowlisted but Never Emitted to the Engine CLI

`default_max_tokens` MUST be added to the model config `[args]` allowlist (`_ARGS_ALLOWLIST`) so per-model configs can set it without being rejected at registry-scan time. `build_engine_command()` MUST NOT ever emit `default_max_tokens` (or any flag derived from it) into the `llama-server` command line — it is a wrapper-only directive consumed solely for request-body injection.

#### Scenario: Config with default_max_tokens passes validation

- GIVEN a model `config.toml` with `[args].default_max_tokens = 2048`
- WHEN `scan_registry()` validates the config's `[args]` keys
- THEN the model is admitted to the registry (the key is not rejected as unknown)

#### Scenario: default_max_tokens never appears on the engine command line

- GIVEN the same model config with `[args].default_max_tokens = 2048`
- WHEN `build_engine_command()` constructs the `llama-server` invocation for that model
- THEN the resulting command line contains no `default_max_tokens`-derived flag or value

## Non-Goals

- **Total-stream-duration timeout is explicitly out of scope.** `max_tokens` bounds generation *length*, not wall-clock *duration*. A slow-but-actively-generating request can legitimately hold the GPU for many minutes (e.g. 4096 tokens at 3 tok/s ≈ 23 minutes); this is an accepted, known residual exposure, not a defect this capability addresses.

#### Scenario: Long-running but active generation is not interrupted

- GIVEN a request has received the injected default of 4096 tokens and the engine is actively emitting tokens, but slowly (e.g. 3 tok/s)
- WHEN the generation continues for over 20 minutes while still producing output
- THEN the wrapper does NOT abort or timeout the request on duration grounds alone — the request completes normally once the token bound (or a natural EOS) is reached
- AND this behavior is correct per this specification, not a bug: duration-based cutoff is out of scope for this capability

- `WRAPPER_MODE`/persistent pre-load logic, `KEYS_FILE`/auth, and registry scanning mechanics beyond the new allowlist key are unaffected and out of scope.
- No change to engine images, Dockerfiles, or compose configuration.
