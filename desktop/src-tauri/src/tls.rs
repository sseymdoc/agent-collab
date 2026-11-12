//! HTTP 客户端与按连接指定的 CA（`docs/desktop.md`“连接与凭据”）。
//!
//! 缺省信任系统根证书。配对时指定了 CA 的连接只信任那份 CA：系统根证书对它不再生效，主机名照常核对。
//! 没有任何关闭证书或主机名校验的途径。PEM 的解析与指纹不依赖 Tauri 运行时，由单元测试覆盖。

use std::time::Duration;

use base64::Engine;
use sha2::{Digest, Sha256};

use super::IDLE;

const BEGIN: &str = "-----BEGIN CERTIFICATE-----";
const END: &str = "-----END CERTIFICATE-----";

/// 一条连接用的 HTTP 客户端，两个客户端的信任来源相同。
#[derive(Clone)]
pub struct Http {
    /// 常规请求与事件流：60 秒没有收到任何字节即认为连接停滞。
    pub api: reqwest::Client,
    /// 上传文件内容：reqwest 的读超时从请求发出时起算，大文件在慢链路上传不完就会被它掐断；
    /// 这个客户端不带读超时，由每个请求按内容大小自己设总时限。
    pub bulk: reqwest::Client,
}

/// 用户为一条连接选定的 CA：只含证书的 PEM 文本，以及首张证书 DER 的 SHA-256。
pub struct Ca {
    pub pem: String,
    pub fingerprint: String,
}

impl Ca {
    /// 解析用户选取的证书文件，并确认它确实能作为信任根使用。
    pub fn parse(file: &[u8]) -> Result<Self, String> {
        let certs = certificates(file)?;
        let pem: String = certs.iter().map(|der| encode(der)).collect();
        http(Some(&pem))?;
        Ok(Self { pem, fingerprint: fingerprint(&certs[0]) })
    }
}

/// 取出 PEM 文本里的全部证书（DER）。其他类型的块（例如同一个文件里的私钥）一律忽略，不会进入连接记录。
pub fn certificates(pem: &[u8]) -> Result<Vec<Vec<u8>>, String> {
    let text = std::str::from_utf8(pem).map_err(|_| "证书文件不是 PEM 文本".to_string())?;
    let mut certs = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find(BEGIN) {
        let body = &rest[start + BEGIN.len()..];
        let end = body.find(END).ok_or_else(|| "证书文件不完整：缺少 END CERTIFICATE".to_string())?;
        let encoded: String = body[..end].chars().filter(|c| !c.is_whitespace()).collect();
        let der = base64::engine::general_purpose::STANDARD
            .decode(encoded)
            .map_err(|_| "证书内容不是有效的 Base64".to_string())?;
        if der.is_empty() {
            return Err("证书内容为空".into());
        }
        certs.push(der);
        rest = &body[end + END.len()..];
    }
    if certs.is_empty() {
        return Err("文件里没有 PEM 格式的证书".into());
    }
    Ok(certs)
}

/// 证书 DER 的 SHA-256，小写十六进制。
pub fn fingerprint(der: &[u8]) -> String {
    super::hex(&Sha256::digest(der))
}

fn encode(der: &[u8]) -> String {
    let encoded = base64::engine::general_purpose::STANDARD.encode(der);
    let lines: Vec<&str> = encoded.as_bytes().chunks(64).map(|line| std::str::from_utf8(line).unwrap_or_default()).collect();
    format!("{BEGIN}\n{}\n{END}\n", lines.join("\n"))
}

/// 构造一条连接的客户端。`ca_pem` 给出时只信任其中的证书，否则信任系统根证书；其余设置相同：
/// 不跟随重定向，不走系统代理，证书与主机名始终校验。
pub fn http(ca_pem: Option<&str>) -> Result<Http, String> {
    let roots = match ca_pem {
        Some(pem) => Some(certificates(pem.as_bytes())?),
        None => None,
    };
    let build = |idle: Option<Duration>| -> reqwest::Result<reqwest::Client> {
        let mut builder = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .user_agent(concat!("agent-collab-desktop/", env!("CARGO_PKG_VERSION")));
        if let Some(idle) = idle {
            builder = builder.read_timeout(idle);
        }
        if let Some(roots) = &roots {
            builder = builder.tls_built_in_root_certs(false);
            for der in roots {
                builder = builder.add_root_certificate(reqwest::Certificate::from_der(der)?);
            }
        }
        builder.build()
    };
    let failed = |e: reqwest::Error| match ca_pem {
        Some(_) => format!("这份证书不能用作 CA：{e}"),
        None => format!("初始化 HTTP 客户端失败：{e}"),
    };
    Ok(Http { api: build(Some(IDLE)).map_err(failed)?, bulk: build(None).map_err(failed)? })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 自签名的测试 CA（P-256，CN=agent-collab test CA）；私钥已丢弃。
    const TEST_CA: &str = "-----BEGIN CERTIFICATE-----
MIIBljCCATugAwIBAgIUJ/KspWUwZqNq7MWbC2SbhGNUqBowCgYIKoZIzj0EAwIw
HzEdMBsGA1UEAwwUYWdlbnQtY29sbGFiIHRlc3QgQ0EwIBcNMjYxMTExMTY0MDIx
WhgPMjEyNjEwMTgxNjQwMjFaMB8xHTAbBgNVBAMMFGFnZW50LWNvbGxhYiB0ZXN0
IENBMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEqMwnaUbW3LSrICniwbzpC5S3
wdrmCZo28MwM8U3urrERNX/KdCNcmAL8Oyq6H1oPwv0YHBX7W35Y8dJRjw55bKNT
MFEwHQYDVR0OBBYEFFosKKP362VstYz92uEHNYyjX4cRMB8GA1UdIwQYMBaAFFos
KKP362VstYz92uEHNYyjX4cRMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwID
SQAwRgIhAIncOoC4NHhFz7tZlVMldd84nu9dGD70qJ8BgJGkkTJjAiEA+jOURi24
i0He8cZj1FumHofQA//IWuzJbJtg9w2Mniw=
-----END CERTIFICATE-----
";
    const TEST_CA_SHA256: &str = "004a9e478d079942e5c6c2b378aaf835dacce80895ec7d5d7009a4ff659e15a4";

    #[test]
    fn fingerprint_is_the_sha256_of_the_first_certificate() {
        let ca = Ca::parse(TEST_CA.as_bytes()).unwrap();
        assert_eq!(ca.fingerprint, TEST_CA_SHA256);
        assert_eq!(ca.pem, TEST_CA);
        // CRLF 换行、前后的说明文字都不影响结果。
        let noisy = format!("subject=CN=x\r\n{}\r\ntrailing", TEST_CA.replace('\n', "\r\n"));
        assert_eq!(Ca::parse(noisy.as_bytes()).unwrap().fingerprint, TEST_CA_SHA256);
    }

    #[test]
    fn bundles_keep_every_certificate_and_drop_everything_else() {
        let key = "-----BEGIN PRIVATE KEY-----\nc2VjcmV0IGtleSBtYXRlcmlhbA==\n-----END PRIVATE KEY-----\n";
        let bundle = format!("{key}{TEST_CA}{key}{TEST_CA}");
        assert_eq!(certificates(bundle.as_bytes()).unwrap().len(), 2);
        let ca = Ca::parse(bundle.as_bytes()).unwrap();
        assert_eq!(ca.fingerprint, TEST_CA_SHA256);
        assert_eq!(ca.pem, format!("{TEST_CA}{TEST_CA}"));
        assert!(!ca.pem.contains("PRIVATE KEY") && !ca.pem.contains("c2VjcmV0"));
    }

    #[test]
    fn files_that_are_not_usable_certificates_are_rejected() {
        let wrap = |body: &str| format!("{BEGIN}\n{body}\n{END}\n");
        for bad in [
            String::new(),
            "just some text".to_string(),
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n".to_string(),
            format!("{BEGIN}\nAAAA\n"),
            wrap("not base64 !!"),
            wrap(""),
            wrap("aGVsbG8gd29ybGQ="), // 合法的 Base64，但不是证书
        ] {
            assert!(Ca::parse(bad.as_bytes()).is_err(), "{bad:?}");
        }
        assert!(Ca::parse(&[0xff, 0xfe, 0x00]).is_err());
    }

    #[test]
    fn clients_build_with_and_without_a_custom_ca() {
        assert!(http(None).is_ok());
        assert!(http(Some(TEST_CA)).is_ok());
        // 记录里的证书坏了就不能建立连接，不会退回系统根证书。
        assert!(http(Some("")).is_err());
        assert!(http(Some("garbage")).is_err());
    }
}
