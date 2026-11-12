//! 信任边界上的纯校验函数：controller 地址、请求路径、blob 摘要、各种 ID、文件名、窗口导航。
//! 不依赖 Tauri 运行时，全部由单元测试覆盖。

use reqwest::Url;

pub const API_PREFIX: &str = "/api/v1";

/// 校验用户输入的 controller 地址，返回规范化的源（`scheme://host[:port]`，不带路径）。
///
/// 只接受 `https://`；`http://` 仅限 loopback 主机。不得带用户名密码、查询串、片段；
/// 路径只能为空或 `/api/v1`。
pub fn base_url(input: &str) -> Result<String, String> {
    let url = Url::parse(input.trim()).map_err(|_| "地址格式无效".to_string())?;
    let loopback = match url.host() {
        Some(url::Host::Domain(d)) => d == "localhost",
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        None => return Err("地址缺少主机名".into()),
    };
    match url.scheme() {
        "https" => {}
        "http" if loopback => {}
        "http" => return Err("http:// 只接受 loopback 主机（127.0.0.1、::1、localhost），其余地址必须是 https://".into()),
        _ => return Err("地址必须以 https:// 开头".into()),
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("地址不得包含用户名或密码".into());
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err("地址不得包含查询串或片段".into());
    }
    if !matches!(url.path(), "" | "/" | "/api/v1" | "/api/v1/") {
        return Err("地址的路径只能为空或 /api/v1".into());
    }
    Ok(url.origin().ascii_serialization())
}

/// `api_request` 的 path：以 `/` 开头、不含 `..`，可带查询串。
pub fn api_path(path: &str) -> Result<(), String> {
    if !path.starts_with('/') {
        return Err("path 必须以 / 开头".into());
    }
    if path.contains("..") {
        return Err("path 不得包含 ..".into());
    }
    if path.chars().any(|c| c.is_control() || c.is_whitespace() || c == '\\' || c == '#') {
        return Err("path 含有不允许的字符".into());
    }
    Ok(())
}

/// 拼出 `<base>/api/v1<path>`，并确认解析后的 URL 仍在同一个源的 `/api/v1/` 之下
/// （URL 解析会把 `%2e%2e` 当作 `..` 处理，所以必须在解析之后再核对一次）。
pub fn api_url(base: &str, path: &str) -> Result<Url, String> {
    api_path(path)?;
    let origin = Url::parse(base).map_err(|_| "连接地址无效".to_string())?;
    let url = Url::parse(&format!("{base}{API_PREFIX}{path}")).map_err(|_| "path 无效".to_string())?;
    if url.origin() != origin.origin() || !url.path().starts_with("/api/v1/") || url.fragment().is_some() {
        return Err("path 超出 /api/v1 范围".into());
    }
    Ok(url)
}

/// `api_request` 允许的方法。
pub fn method(method: &str) -> bool {
    matches!(method, "GET" | "POST" | "PUT" | "PATCH" | "DELETE")
}

/// blob 摘要：64 位小写十六进制。
pub fn sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

/// controller 返回的 ID（`controller_id`、`client_id`）：进入凭据账户名、连接文件和响应头比较，
/// 只接受短的 ASCII 标识符。
pub fn ident(s: &str) -> bool {
    (1..=128).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.'))
}

/// 小写 UUID（`8-4-4-4-12`）。项目、任务、检查点、快照与应用的 ID 都是这个形状；
/// 它们进入请求路径与本机文件名，所以页面给的值先过这一关。
pub fn uuid(s: &str) -> bool {
    s.len() == 36
        && s.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => matches!(b, b'0'..=b'9' | b'a'..=b'f'),
        })
}

/// 节点 ID：小写字母、数字和连字符，最长 63，不以连字符开头（与 controller 的规则相同）。
pub fn node_id(s: &str) -> bool {
    (1..=63).contains(&s.len())
        && !s.starts_with('-')
        && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// 一次性配对码：短的可见 ASCII，不以连字符开头（不会被命令行当成选项）。
pub fn pairing_code(s: &str) -> bool {
    (1..=256).contains(&s.len()) && !s.starts_with('-') && s.bytes().all(|b| b.is_ascii_graphic())
}

/// 页面给出的排除规则列表：只限制数量与长度，规则本身由本机助手校验。
pub fn excludes(patterns: &[String]) -> bool {
    patterns.len() <= 2000 && patterns.iter().all(|p| !p.is_empty() && p.len() <= 1024 && !p.chars().any(char::is_control))
}

/// controller 签发的凭据：可见 ASCII，能放进 `Authorization` 头。
pub fn token(s: &str) -> bool {
    (1..=4096).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_graphic())
}

/// “另存为”对话框的缺省文件名：取最后一段路径，去掉分隔符与各平台保留字符。
pub fn file_name(name: &str) -> String {
    let last = name.rsplit(['/', '\\']).next().unwrap_or("");
    let cleaned: String = last
        .chars()
        .filter(|c| !c.is_control() && !matches!(c, ':' | '*' | '?' | '"' | '<' | '>' | '|'))
        .take(200)
        .collect();
    cleaned.trim_matches([' ', '.']).to_string()
}

/// 主窗口只允许停留在随包分发的页面上：macOS/Linux 为 `tauri://localhost`，
/// Windows 为 `http://tauri.localhost`（`tauri.conf.json` 没有开 `useHttpsScheme`，也没有 `devUrl`，
/// 开发运行同样从 frontendDist 加载）。协议与端口都要精确匹配：
/// 带端口的同名主机是本机上任意进程都能监听的普通网页，不是随包页面。
pub fn is_app_url(url: &Url) -> bool {
    url.port().is_none()
        && matches!((url.scheme(), url.host_str()), ("tauri", Some("localhost")) | ("http", Some("tauri.localhost")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base_url_accepts_https_and_loopback_http() {
        for (input, want) in [
            ("https://example.com", "https://example.com"),
            ("https://example.com/", "https://example.com"),
            ("  https://example.com/api/v1  ", "https://example.com"),
            ("https://example.com:8443/api/v1/", "https://example.com:8443"),
            ("HTTPS://Example.COM", "https://example.com"),
            ("https://example.com:443", "https://example.com"),
            ("https://10.0.0.5:8443", "https://10.0.0.5:8443"),
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
            ("http://localhost:8080/api/v1", "http://localhost:8080"),
            ("http://[::1]:8080", "http://[::1]:8080"),
        ] {
            assert_eq!(base_url(input).as_deref(), Ok(want), "{input}");
        }
    }

    #[test]
    fn base_url_rejects_everything_else() {
        for input in [
            "",
            "example.com",
            "//example.com",
            "ftp://example.com",
            "file:///etc/passwd",
            "http://example.com",
            "http://10.0.0.2:8080",
            "http://0.0.0.0:8080",
            "http://localhost.evil.com",
            "http://127.0.0.1.evil.com",
            "http://foo.localhost",
            "https://user@example.com",
            "https://user:pw@example.com",
            "https://example.com/?a=1",
            "https://example.com/?",
            "https://example.com/#frag",
            "https://example.com/other",
            "https://example.com/api/v1/jobs",
            "https://example.com/api/v2",
            "https://",
        ] {
            assert!(base_url(input).is_err(), "{input}");
        }
    }

    #[test]
    fn api_path_rules() {
        for ok in ["/jobs", "/jobs?status=queued&limit=5", "/jobs/123/cancel", "/blobs/abc", "/"] {
            assert!(api_path(ok).is_ok(), "{ok}");
        }
        for bad in ["", "jobs", "/../x", "/a/..", "/a/../b", "/jobs?x=..", "/a b", "/a\\b", "/a#b", "/a\nb", "https://evil.example/x"] {
            assert!(api_path(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn api_url_stays_under_api_prefix() {
        let base = "https://example.com:8443";
        assert_eq!(
            api_url(base, "/jobs?limit=5").unwrap().as_str(),
            "https://example.com:8443/api/v1/jobs?limit=5"
        );
        // 双斜杠不会换主机。
        assert_eq!(
            api_url(base, "//evil.example/x").unwrap().host_str(),
            Some("example.com")
        );
        // 百分号编码的点段会被 URL 解析折叠，必须拒绝。
        for bad in ["/%2e%2e/%2e%2e/admin", "/%2E%2E/x", "/.%2e/x", "/jobs/../x"] {
            assert!(api_url(base, bad).is_err(), "{bad}");
        }
        assert!(api_url("http://[::1]:8080", "/client").is_ok());
    }

    #[test]
    fn method_allowlist() {
        assert!(["GET", "POST", "PUT", "PATCH", "DELETE"].iter().all(|m| method(m)));
        assert!(!["get", "CONNECT", "TRACE", "", "GET /x"].iter().any(|m| method(m)));
    }

    #[test]
    fn sha256_ident_and_token_formats() {
        assert!(sha256_hex(&"a1".repeat(32)));
        assert!(!sha256_hex(&"A1".repeat(32)));
        assert!(!sha256_hex(&"a".repeat(63)));
        assert!(!sha256_hex(&"g".repeat(64)));
        assert!(!sha256_hex(&format!("{}/..", "a".repeat(61))));

        assert!(ident("3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e11"));
        assert!(!ident(""));
        assert!(!ident("a/b"));
        assert!(!ident("a b"));
        assert!(!ident(&"a".repeat(129)));

        assert!(token("ac_client_Abc-123.xyz"));
        assert!(!token(""));
        assert!(!token("a b"));
        assert!(!token("a\r\nX-Injected: 1"));
    }

    #[test]
    fn uuid_must_be_lowercase_and_hyphenated() {
        assert!(uuid("3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e11"));
        for bad in [
            "",
            "3F2B6C1E-0C8E-4C57-9A43-1F6F0C9B7E11",
            "3f2b6c1e0c8e4c579a431f6f0c9b7e11",
            "3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e1",
            "3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e111",
            "3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e1g",
            "3f2b6c1e/0c8e-4c57-9a43-1f6f0c9b7e11",
            "../../../../../../../../../etc/passwd",
            "{3f2b6c1e-0c8e-4c57-9a43-1f6f0c9b7e1}",
        ] {
            assert!(!uuid(bad), "{bad}");
        }
    }

    #[test]
    fn values_passed_to_the_installer_cannot_look_like_options() {
        assert!(node_id("node-mac-1") && node_id("a") && node_id(&"a".repeat(63)));
        for bad in ["", "-node", "Node", "node_1", "node 1", "节点", &"a".repeat(64)] {
            assert!(!node_id(bad), "{bad}");
        }
        assert!(pairing_code("3f9a0c1d2e4b5a6978") && pairing_code("AbC_123-xyz"));
        for bad in ["", "-code", "--uninstall", "a b", "a\nb", "码", &"a".repeat(257)] {
            assert!(!pairing_code(bad), "{bad}");
        }
    }

    #[test]
    fn exclude_lists_are_bounded() {
        let list = |items: &[&str]| items.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(excludes(&[]));
        assert!(excludes(&list(&[".git/**", "*.pem", "目录/**"])));
        assert!(!excludes(&list(&[""])));
        assert!(!excludes(&list(&["a\nb"])));
        assert!(!excludes(&["x".repeat(1025)]));
        assert!(!excludes(&vec!["x".to_string(); 2001]));
    }

    #[test]
    fn file_name_is_a_single_safe_component() {
        assert_eq!(file_name("report.md"), "report.md");
        assert_eq!(file_name("src/app/main.rs"), "main.rs");
        assert_eq!(file_name("C:\\Users\\x\\out.txt"), "out.txt");
        assert_eq!(file_name("../../etc/passwd"), "passwd");
        assert_eq!(file_name("a:b*c?.txt"), "abc.txt");
        assert_eq!(file_name(".."), "");
        assert_eq!(file_name("dir/"), "");
        assert_eq!(file_name(""), "");
    }

    #[test]
    fn only_bundled_pages_are_navigable() {
        let ok = |s: &str| is_app_url(&Url::parse(s).unwrap());
        assert!(ok("tauri://localhost/"));
        assert!(ok("tauri://localhost/assets/index.js"));
        assert!(ok("http://tauri.localhost/"));
        assert!(ok("http://tauri.localhost/index.html"));
        assert!(ok("http://tauri.localhost:80/")); // 缺省端口会被规范化掉
        assert!(!ok("https://tauri.localhost/index.html"));
        assert!(!ok("http://tauri.localhost:8080/"));
        assert!(!ok("tauri://localhost:8080/"));
        assert!(!ok("https://example.com/"));
        assert!(!ok("http://localhost:8080/"));
        assert!(!ok("http://tauri.localhost.evil.com/"));
        assert!(!ok("tauri://evil/"));
        assert!(!ok("file:///C:/x.html"));
        assert!(!ok("about:blank"));
    }
}
