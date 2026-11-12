# 桌面外壳的 Rust 依赖

由 `desktop/src-tauri/Cargo.lock` 经 `cargo metadata --filter-platform` 生成，列出进入各目标构建的 crate（含间接依赖）与 Cargo.toml 里声明的许可证。目标：Windows x64、macOS arm64。共 324 个。

MPL-2.0 的 crate（文件级著佐权）未作修改，源码见 crates.io 上对应版本；分发安装包时随附本清单与各 crate 的许可证文本。

| crate | 版本 | 许可证 | 目标 |
| --- | --- | --- | --- |
| adler2 | 2.0.1 | 0BSD OR MIT OR Apache-2.0 | 全部 |
| aho-corasick | 1.1.5 | Unlicense OR MIT | 全部 |
| alloc-no-stdlib | 3.0.0 | BSD-3-Clause | 全部 |
| alloc-stdlib | 0.3.0 | BSD-3-Clause | 全部 |
| anyhow | 1.0.104 | MIT OR Apache-2.0 | 全部 |
| atomic-waker | 1.1.2 | Apache-2.0 OR MIT | 全部 |
| autocfg | 1.5.1 | Apache-2.0 OR MIT | 全部 |
| base64 | 0.21.7 | MIT OR Apache-2.0 | macOS arm64 |
| base64 | 0.22.1 | MIT OR Apache-2.0 | 全部 |
| base64 | 0.23.1 | MIT OR Apache-2.0 | 全部 |
| bit-set | 0.8.0 | Apache-2.0 OR MIT | 全部 |
| bit-vec | 0.8.0 | Apache-2.0 OR MIT | 全部 |
| bitflags | 2.13.2 | MIT OR Apache-2.0 | 全部 |
| block-buffer | 0.10.4 | MIT OR Apache-2.0 | 全部 |
| block2 | 0.6.2 | MIT | macOS arm64 |
| brotli | 9.1.1 | BSD-3-Clause AND MIT | 全部 |
| brotli-decompressor | 6.3.1 | BSD-3-Clause/MIT | 全部 |
| bs58 | 0.5.1 | MIT/Apache-2.0 | 全部 |
| byteorder | 1.5.0 | Unlicense OR MIT | 全部 |
| bytes | 1.12.1 | MIT | 全部 |
| camino | 1.2.6 | MIT OR Apache-2.0 | 全部 |
| cargo-platform | 0.1.9 | MIT OR Apache-2.0 | 全部 |
| cargo_metadata | 0.19.2 | MIT | 全部 |
| cargo_toml | 1.0.1 | Apache-2.0 OR MIT | 全部 |
| cc | 1.11.1 | MIT OR Apache-2.0 | 全部 |
| cfb | 0.14.0 | MIT | 全部 |
| cfg-if | 1.0.5 | MIT OR Apache-2.0 | 全部 |
| cfg_aliases | 0.2.2 | MIT | 全部 |
| chacha20 | 0.10.2 | MIT OR Apache-2.0 | 全部 |
| chrono | 0.4.45 | MIT OR Apache-2.0 | 全部 |
| cookie | 0.18.2 | MIT OR Apache-2.0 | 全部 |
| core-foundation | 0.10.1 | MIT OR Apache-2.0 | macOS arm64 |
| core-foundation-sys | 0.8.7 | MIT OR Apache-2.0 | macOS arm64 |
| core-graphics | 0.25.0 | MIT OR Apache-2.0 | macOS arm64 |
| core-graphics-types | 0.2.0 | MIT OR Apache-2.0 | macOS arm64 |
| cpufeatures | 0.2.17 | MIT OR Apache-2.0 | 全部 |
| cpufeatures | 0.3.1 | MIT OR Apache-2.0 | Windows x64 |
| crc32fast | 1.5.5 | MIT OR Apache-2.0 | 全部 |
| crossbeam-channel | 0.5.17 | MIT OR Apache-2.0 | 全部 |
| crossbeam-utils | 0.8.23 | MIT OR Apache-2.0 | 全部 |
| crypto-common | 0.1.7 | MIT OR Apache-2.0 | 全部 |
| cssparser | 0.37.0 | MPL-2.0 | 全部 |
| cssparser-macros | 0.7.1 | MPL-2.0 | 全部 |
| ctor | 1.0.13 | Apache-2.0 OR MIT | 全部 |
| darling | 0.24.1 | MIT | 全部 |
| darling_core | 0.24.1 | MIT | 全部 |
| darling_macro | 0.24.1 | MIT | 全部 |
| deranged | 0.5.9 | MIT OR Apache-2.0 | 全部 |
| derive_more | 2.1.1 | MIT | 全部 |
| derive_more-impl | 2.1.1 | MIT | 全部 |
| digest | 0.10.7 | MIT OR Apache-2.0 | 全部 |
| dirs | 7.0.0 | MIT OR Apache-2.0 | 全部 |
| dirs-sys | 0.5.0 | MIT OR Apache-2.0 | 全部 |
| dispatch2 | 0.3.1 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| displaydoc | 0.2.7 | MIT OR Apache-2.0 | 全部 |
| dom_query | 0.28.0 | MIT | 全部 |
| dpi | 0.1.2 | Apache-2.0 AND MIT | 全部 |
| dtoa | 1.0.11 | MIT OR Apache-2.0 | 全部 |
| dtoa-short | 0.3.5 | MPL-2.0 | 全部 |
| dunce | 1.0.5 | CC0-1.0 OR MIT-0 OR Apache-2.0 | 全部 |
| dyn-clone | 1.0.20 | MIT OR Apache-2.0 | 全部 |
| embed-resource | 3.0.12 | MIT | 全部 |
| embed_plist | 1.2.2 | MIT OR Apache-2.0 | macOS arm64 |
| equivalent | 1.0.3 | Apache-2.0 OR MIT | 全部 |
| erased-serde | 0.4.11 | MIT OR Apache-2.0 | 全部 |
| fastrand | 2.5.0 | Apache-2.0 OR MIT | 全部 |
| fdeflate | 0.3.7 | MIT OR Apache-2.0 | 全部 |
| find-msvc-tools | 0.1.15 | MIT OR Apache-2.0 | 全部 |
| flate2 | 1.1.11 | MIT OR Apache-2.0 | 全部 |
| fnv | 1.0.7 | Apache-2.0 / MIT | 全部 |
| foldhash | 0.2.0 | Zlib | 全部 |
| foreign-types | 0.5.0 | MIT/Apache-2.0 | macOS arm64 |
| foreign-types-macros | 0.2.4 | MIT/Apache-2.0 | macOS arm64 |
| foreign-types-shared | 0.3.1 | MIT/Apache-2.0 | macOS arm64 |
| form_urlencoded | 1.2.2 | MIT OR Apache-2.0 | 全部 |
| futures-channel | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-core | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-io | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-macro | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-sink | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-task | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| futures-util | 0.3.34 | MIT OR Apache-2.0 | 全部 |
| generic-array | 0.14.7 | MIT | 全部 |
| getrandom | 0.2.17 | MIT OR Apache-2.0 | 全部 |
| getrandom | 0.3.4 | MIT OR Apache-2.0 | 全部 |
| getrandom | 0.4.3 | MIT OR Apache-2.0 | 全部 |
| glob | 0.3.4 | MIT OR Apache-2.0 | 全部 |
| hashbrown | 0.12.3 | MIT OR Apache-2.0 | 全部 |
| hashbrown | 0.17.1 | MIT OR Apache-2.0 | 全部 |
| heck | 0.5.0 | MIT OR Apache-2.0 | 全部 |
| hex | 0.4.3 | MIT OR Apache-2.0 | 全部 |
| html5ever | 0.39.0 | MIT OR Apache-2.0 | 全部 |
| http | 1.5.0 | MIT OR Apache-2.0 | 全部 |
| http-body | 1.1.0 | MIT | 全部 |
| http-body-util | 0.1.5 | MIT | 全部 |
| httparse | 1.10.1 | MIT OR Apache-2.0 | 全部 |
| hyper | 1.12.0 | MIT | 全部 |
| hyper-rustls | 0.27.10 | Apache-2.0 OR ISC OR MIT | 全部 |
| hyper-util | 0.1.21 | MIT | 全部 |
| iana-time-zone | 0.1.65 | MIT OR Apache-2.0 | macOS arm64 |
| ico | 0.6.0 | MIT | 全部 |
| icu_collections | 2.4.0 | Unicode-3.0 | 全部 |
| icu_locale_core | 2.4.0 | Unicode-3.0 | 全部 |
| icu_normalizer | 2.4.0 | Unicode-3.0 | 全部 |
| icu_normalizer_data | 2.4.0 | Unicode-3.0 | 全部 |
| icu_properties | 2.4.0 | Unicode-3.0 | 全部 |
| icu_properties_data | 2.4.0 | Unicode-3.0 | 全部 |
| icu_provider | 2.4.0 | Unicode-3.0 | 全部 |
| ident_case | 1.0.1 | MIT/Apache-2.0 | 全部 |
| idna | 1.1.0 | MIT OR Apache-2.0 | 全部 |
| idna_adapter | 1.2.2 | Apache-2.0 OR MIT | 全部 |
| indexmap | 1.9.3 | Apache-2.0 OR MIT | 全部 |
| indexmap | 2.14.2 | Apache-2.0 OR MIT | 全部 |
| infer | 0.22.0 | MIT | 全部 |
| ipnet | 2.12.2 | MIT OR Apache-2.0 | 全部 |
| itoa | 1.0.19 | MIT OR Apache-2.0 | 全部 |
| jiff | 0.2.42 | Unlicense OR MIT | 全部 |
| jiff-core | 0.1.4 | Unlicense OR MIT | 全部 |
| jiff-tzdb | 0.1.9 | Unlicense OR MIT | Windows x64 |
| jiff-tzdb-platform | 0.1.3 | Unlicense OR MIT | Windows x64 |
| json-patch | 4.2.0 | MIT/Apache-2.0 | 全部 |
| jsonptr | 0.7.1 | MIT OR Apache-2.0 | 全部 |
| keyboard-types | 0.8.3 | MIT OR Apache-2.0 | 全部 |
| keyring | 3.6.3 | MIT OR Apache-2.0 | 全部 |
| libc | 0.2.191 | MIT OR Apache-2.0 | 全部 |
| litemap | 0.8.3 | Unicode-3.0 | 全部 |
| lock_api | 0.4.14 | MIT OR Apache-2.0 | 全部 |
| log | 0.4.35 | MIT OR Apache-2.0 | 全部 |
| lru-slab | 0.1.3 | MIT OR Apache-2.0 OR Zlib | 全部 |
| mac-notification-sys | 0.6.15 | MIT/Apache-2.0 | macOS arm64 |
| markup5ever | 0.39.0 | MIT OR Apache-2.0 | 全部 |
| memchr | 2.8.3 | Unlicense OR MIT | 全部 |
| mime | 0.3.18 | MIT OR Apache-2.0 | 全部 |
| miniz_oxide | 0.8.9 | MIT OR Zlib OR Apache-2.0 | 全部 |
| miniz_oxide | 0.9.1 | MIT OR Zlib OR Apache-2.0 | 全部 |
| mio | 1.2.4 | MIT | 全部 |
| muda | 0.20.0 | Apache-2.0 OR MIT | 全部 |
| new_debug_unreachable | 1.0.6 | MIT | 全部 |
| notify-rust | 4.19.0 | MIT OR Apache-2.0 | 全部 |
| num-conv | 0.2.2 | MIT OR Apache-2.0 | 全部 |
| num-traits | 0.2.19 | MIT OR Apache-2.0 | 全部 |
| objc2 | 0.6.5 | MIT | macOS arm64 |
| objc2-app-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-core-foundation | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-core-graphics | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-encode | 4.1.0 | MIT | macOS arm64 |
| objc2-exception-helper | 0.1.1 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-foundation | 0.3.2 | MIT | macOS arm64 |
| objc2-io-surface | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-quartz-core | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| objc2-web-kit | 0.3.2 | Zlib OR Apache-2.0 OR MIT | macOS arm64 |
| once_cell | 1.21.4 | MIT OR Apache-2.0 | 全部 |
| option-ext | 0.2.0 | MPL-2.0 | 全部 |
| parking_lot | 0.12.5 | MIT OR Apache-2.0 | 全部 |
| parking_lot_core | 0.9.12 | MIT OR Apache-2.0 | 全部 |
| percent-encoding | 2.3.2 | MIT OR Apache-2.0 | 全部 |
| phf | 0.13.1 | MIT | 全部 |
| phf_codegen | 0.13.1 | MIT | 全部 |
| phf_generator | 0.13.1 | MIT | 全部 |
| phf_macros | 0.13.1 | MIT | 全部 |
| phf_shared | 0.13.1 | MIT | 全部 |
| pin-project-lite | 0.2.17 | Apache-2.0 OR MIT | 全部 |
| plist | 1.10.3 | MIT | 全部 |
| png | 0.18.1 | MIT OR Apache-2.0 | 全部 |
| potential_utf | 0.1.6 | Unicode-3.0 | 全部 |
| powerfmt | 0.2.1 | MIT OR Apache-2.0 | 全部 |
| precomputed-hash | 0.1.1 | MIT | 全部 |
| proc-macro2 | 1.0.107 | MIT OR Apache-2.0 | 全部 |
| quick-xml | 0.42.0 | MIT | 全部 |
| quinn | 0.11.12 | MIT OR Apache-2.0 | 全部 |
| quinn-proto | 0.11.19 | MIT OR Apache-2.0 | 全部 |
| quinn-udp | 0.5.16 | MIT OR Apache-2.0 | 全部 |
| quote | 1.0.47 | MIT OR Apache-2.0 | 全部 |
| rand | 0.10.3 | MIT OR Apache-2.0 | 全部 |
| rand_core | 0.10.1 | MIT OR Apache-2.0 | 全部 |
| rand_pcg | 0.10.2 | MIT OR Apache-2.0 | 全部 |
| raw-window-handle | 0.6.2 | MIT OR Apache-2.0 OR Zlib | 全部 |
| ref-cast | 1.0.27 | MIT OR Apache-2.0 | 全部 |
| ref-cast-impl | 1.0.27 | MIT OR Apache-2.0 | 全部 |
| regex | 1.13.1 | MIT OR Apache-2.0 | 全部 |
| regex-automata | 0.4.18 | MIT OR Apache-2.0 | 全部 |
| regex-syntax | 0.8.11 | MIT OR Apache-2.0 | 全部 |
| reqwest | 0.12.28 | MIT OR Apache-2.0 | 全部 |
| rfd | 0.17.2 | MIT | 全部 |
| ring | 0.17.14 | Apache-2.0 AND ISC | 全部 |
| rustc-hash | 2.1.3 | Apache-2.0 OR MIT | 全部 |
| rustc_version | 0.4.1 | MIT OR Apache-2.0 | 全部 |
| rustls | 0.23.46 | Apache-2.0 OR ISC OR MIT | 全部 |
| rustls-native-certs | 0.8.5 | Apache-2.0 OR ISC OR MIT | 全部 |
| rustls-pki-types | 1.16.0 | MIT OR Apache-2.0 | 全部 |
| rustls-webpki | 0.103.16 | ISC | 全部 |
| ryu | 1.0.23 | Apache-2.0 OR BSL-1.0 | 全部 |
| same-file | 1.0.6 | Unlicense/MIT | 全部 |
| schannel | 0.1.30 | MIT | Windows x64 |
| schemars | 0.8.22 | MIT | 全部 |
| schemars | 0.9.0 | MIT | 全部 |
| schemars | 1.2.3 | MIT | 全部 |
| schemars_derive | 0.8.22 | MIT | 全部 |
| scopeguard | 1.2.0 | MIT OR Apache-2.0 | 全部 |
| security-framework | 3.7.0 | MIT OR Apache-2.0 | macOS arm64 |
| security-framework-sys | 2.17.0 | MIT OR Apache-2.0 | macOS arm64 |
| selectors | 0.38.0 | MPL-2.0 | 全部 |
| semver | 1.0.28 | MIT OR Apache-2.0 | 全部 |
| serde | 1.0.229 | MIT OR Apache-2.0 | 全部 |
| serde-untagged | 0.1.9 | MIT OR Apache-2.0 | 全部 |
| serde_core | 1.0.229 | MIT OR Apache-2.0 | 全部 |
| serde_derive | 1.0.229 | MIT OR Apache-2.0 | 全部 |
| serde_derive_internals | 0.29.1 | MIT OR Apache-2.0 | 全部 |
| serde_json | 1.0.154 | MIT OR Apache-2.0 | 全部 |
| serde_repr | 0.1.21 | MIT OR Apache-2.0 | 全部 |
| serde_spanned | 1.1.3 | MIT OR Apache-2.0 | 全部 |
| serde_urlencoded | 0.7.1 | MIT/Apache-2.0 | 全部 |
| serde_with | 3.24.0 | MIT OR Apache-2.0 | 全部 |
| serde_with_macros | 3.24.0 | MIT OR Apache-2.0 | 全部 |
| serialize-to-javascript | 0.1.2 | MIT OR Apache-2.0 | 全部 |
| serialize-to-javascript-impl | 0.1.2 | MIT OR Apache-2.0 | 全部 |
| servo_arc | 0.4.3 | MIT OR Apache-2.0 | 全部 |
| sha2 | 0.10.9 | MIT OR Apache-2.0 | 全部 |
| shlex | 2.0.1 | MIT OR Apache-2.0 | 全部 |
| simd-adler32 | 0.3.10 | MIT | 全部 |
| siphasher | 1.0.4 | MIT OR Apache-2.0 | 全部 |
| slab | 0.4.12 | MIT | 全部 |
| smallvec | 1.16.5 | MIT OR Apache-2.0 | 全部 |
| socket2 | 0.6.5 | MIT OR Apache-2.0 | 全部 |
| softbuffer | 0.4.8 | MIT OR Apache-2.0 | Windows x64 |
| stable_deref_trait | 1.2.1 | MIT OR Apache-2.0 | 全部 |
| static_vcruntime | 3.0.0 | MIT OR Apache-2.0 OR Zlib | 全部 |
| string_cache | 0.9.0 | MIT OR Apache-2.0 | 全部 |
| string_cache_codegen | 0.6.1 | MIT OR Apache-2.0 | 全部 |
| strsim | 0.11.1 | MIT | 全部 |
| subtle | 2.6.1 | BSD-3-Clause | 全部 |
| swift-rs | 1.0.9 | MIT OR Apache-2.0 | macOS arm64 |
| syn | 2.0.119 | MIT OR Apache-2.0 | 全部 |
| syn | 3.0.10 | MIT OR Apache-2.0 | 全部 |
| sync_wrapper | 1.0.2 | Apache-2.0 | 全部 |
| synstructure | 0.14.0 | MIT | 全部 |
| tao | 0.37.2 | Apache-2.0 | 全部 |
| tauri | 2.12.5 | Apache-2.0 OR MIT | 全部 |
| tauri-build | 2.7.4 | Apache-2.0 OR MIT | 全部 |
| tauri-codegen | 2.7.4 | Apache-2.0 OR MIT | 全部 |
| tauri-macros | 2.7.4 | Apache-2.0 OR MIT | 全部 |
| tauri-plugin | 2.7.2 | Apache-2.0 OR MIT | 全部 |
| tauri-plugin-notification | 2.8.1 | Apache-2.0 OR MIT | 全部 |
| tauri-runtime | 2.12.2 | Apache-2.0 OR MIT | 全部 |
| tauri-runtime-wry | 2.12.3 | Apache-2.0 OR MIT | 全部 |
| tauri-utils | 2.10.2 | Apache-2.0 OR MIT | 全部 |
| tauri-winres | 0.3.7 | MIT | 全部 |
| tauri-winrt-notification | 0.8.1 | MIT OR Apache-2.0 | Windows x64 |
| tendril | 0.5.1 | MIT OR Apache-2.0 | 全部 |
| thiserror | 2.0.21 | MIT OR Apache-2.0 | 全部 |
| thiserror-impl | 2.0.21 | MIT OR Apache-2.0 | 全部 |
| time | 0.3.55 | MIT OR Apache-2.0 | 全部 |
| time-core | 0.1.9 | MIT OR Apache-2.0 | 全部 |
| time-macros | 0.2.32 | MIT OR Apache-2.0 | 全部 |
| tinystr | 0.8.5 | Unicode-3.0 | 全部 |
| tinyvec | 1.13.3 | Zlib OR Apache-2.0 OR MIT | 全部 |
| tokio | 1.54.1 | MIT | 全部 |
| tokio-rustls | 0.26.6 | MIT OR Apache-2.0 | 全部 |
| tokio-util | 0.7.20 | MIT | 全部 |
| toml | 1.2.1+spec-1.1.0 | MIT OR Apache-2.0 | 全部 |
| toml_datetime | 1.1.3+spec-1.1.0 | MIT OR Apache-2.0 | 全部 |
| toml_parser | 1.1.5+spec-1.1.0 | MIT OR Apache-2.0 | 全部 |
| toml_writer | 1.2.0+spec-1.1.0 | MIT OR Apache-2.0 | 全部 |
| tower | 0.5.3 | MIT | 全部 |
| tower-http | 0.6.11 | MIT | 全部 |
| tower-layer | 0.3.3 | MIT | 全部 |
| tower-service | 0.3.3 | MIT | 全部 |
| tracing | 0.1.44 | MIT | 全部 |
| tracing-attributes | 0.1.31 | MIT | 全部 |
| tracing-core | 0.1.36 | MIT | 全部 |
| tray-icon | 0.25.1 | MIT OR Apache-2.0 | 全部 |
| try-lock | 0.2.5 | MIT | 全部 |
| typeid | 1.0.3 | MIT OR Apache-2.0 | 全部 |
| typenum | 1.20.1 | MIT OR Apache-2.0 | 全部 |
| unicode-ident | 1.0.27 | (MIT OR Apache-2.0) AND Unicode-3.0 | 全部 |
| unicode-segmentation | 1.13.3 | MIT OR Apache-2.0 | Windows x64 |
| untrusted | 0.9.0 | ISC | 全部 |
| url | 2.5.8 | MIT OR Apache-2.0 | 全部 |
| urlpattern | 0.6.0 | MIT | 全部 |
| utf8_iter | 1.1.0 | Apache-2.0 OR MIT | 全部 |
| utf_types | 1.0.0 | Apache-2.0 OR MIT | 全部 |
| uuid | 1.29.2 | Apache-2.0 OR MIT | 全部 |
| version_check | 0.9.5 | MIT/Apache-2.0 | 全部 |
| vswhom | 0.1.0 | MIT | Windows x64 |
| vswhom-sys | 0.1.3 | MIT | Windows x64 |
| walkdir | 2.5.0 | Unlicense/MIT | 全部 |
| want | 0.3.2 | MIT | 全部 |
| web-time | 1.1.0 | MIT OR Apache-2.0 | 全部 |
| web_atoms | 0.2.6 | MIT OR Apache-2.0 | 全部 |
| webview2-com | 0.39.1 | MIT | Windows x64 |
| webview2-com-macros | 0.8.1 | MIT | Windows x64 |
| webview2-com-sys | 0.39.1 | MIT | Windows x64 |
| winapi-util | 0.1.11 | Unlicense OR MIT | Windows x64 |
| window-vibrancy | 0.8.1 | Apache-2.0 OR MIT | 全部 |
| windows | 0.62.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-collections | 0.3.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-core | 0.62.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-future | 0.3.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-implement | 0.60.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-interface | 0.59.3 | MIT OR Apache-2.0 | Windows x64 |
| windows-link | 0.2.1 | MIT OR Apache-2.0 | Windows x64 |
| windows-numerics | 0.3.1 | MIT OR Apache-2.0 | Windows x64 |
| windows-result | 0.4.1 | MIT OR Apache-2.0 | Windows x64 |
| windows-strings | 0.5.1 | MIT OR Apache-2.0 | Windows x64 |
| windows-sys | 0.60.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-sys | 0.61.2 | MIT OR Apache-2.0 | Windows x64 |
| windows-targets | 0.53.5 | MIT OR Apache-2.0 | Windows x64 |
| windows-threading | 0.2.1 | MIT OR Apache-2.0 | Windows x64 |
| windows-version | 0.1.7 | MIT OR Apache-2.0 | Windows x64 |
| windows_x86_64_msvc | 0.53.1 | MIT OR Apache-2.0 | Windows x64 |
| winnow | 1.0.5 | MIT | 全部 |
| winreg | 0.56.0 | MIT | Windows x64 |
| writeable | 0.6.4 | Unicode-3.0 | 全部 |
| wry | 0.57.2 | Apache-2.0 OR MIT | 全部 |
| yoke | 0.8.4 | Unicode-3.0 | 全部 |
| yoke-derive | 0.8.4 | Unicode-3.0 | 全部 |
| zerofrom | 0.1.8 | Unicode-3.0 | 全部 |
| zerofrom-derive | 0.1.8 | Unicode-3.0 | 全部 |
| zeroize | 1.9.1 | Apache-2.0 OR MIT | 全部 |
| zerotrie | 0.2.5 | Unicode-3.0 | 全部 |
| zerovec | 0.11.9 | Unicode-3.0 | 全部 |
| zerovec-derive | 0.11.7 | Unicode-3.0 | 全部 |
| zlib-rs | 0.6.8 | Zlib | 全部 |
| zmij | 1.0.23 | MIT | 全部 |
