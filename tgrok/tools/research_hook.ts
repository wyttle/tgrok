// omp 研究员进程加载的 hook：read 只准读公网 http(s) 网页。
// 拦掉本地文件、omp 的内部地址（artifact:// local:// cfg:// 等）以及 localhost、内网和云元数据地址，
// 防止群友用提示词注入让研究员读出凭据或服务器上的数据。域名解析到内网（DNS rebinding）的情况拦不住。

interface ToolCallEvent {
  toolName: string;
  input: Record<string, unknown>;
}

type ToolCallResult = { block: true; reason: string } | undefined;

// omp hook API 里本文件用到的部分
interface HookApi {
  on(event: "tool_call", handler: (event: ToolCallEvent) => Promise<ToolCallResult>): void;
}

const BLOCKED = "only public http(s) web pages may be read";

function isPrivateHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) {
    return true;
  }
  const v4 = host.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
  if (v4) {
    const a = Number(v4[1]);
    const b = Number(v4[2]);
    return a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  if (host.includes(":")) {
    // IPv6：回环、未指定、唯一本地 fc00::/7、链路本地 fe80::/10、内嵌 IPv4
    return host === "::1" || host === "::" || /^f[cd]/.test(host) || /^fe[89ab]/.test(host) || host.startsWith("::ffff:");
  }
  return false;
}

export default function (pi: HookApi): void {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "read") return undefined;
    const path = event.input.path;
    let raw = typeof path === "string" ? path.trim() : "";
    if (raw.toLowerCase().startsWith("www.")) raw = "https://" + raw;
    let url: URL;
    try {
      url = new URL(raw);
    } catch {
      return { block: true, reason: BLOCKED };
    }
    if ((url.protocol !== "http:" && url.protocol !== "https:") || isPrivateHost(url.hostname)) {
      return { block: true, reason: BLOCKED };
    }
    return undefined;
  });
}
