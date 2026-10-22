import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, setToken } from "../api/client";

export function Login() {
  const config = useQuery({ queryKey: ["auth-config"], queryFn: () => api<{ totp: boolean }>("/api/auth/config") });
  const totp = config.data?.totp ?? config.isError; // 查不到設定時寧可顯示欄位，避免卡在登不進去
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const { token } = await api<{ token: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password, code }),
      });
      setToken(token);
    } catch (err) {
      setError((err as Error).message);
      setCode(""); // 驗證碼每 30 秒換一次，失敗後清掉讓人重新輸入
    }
  };
  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <h1>TradeBot 登入</h1>
        <label className="field">帳號<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label>
        <label className="field">密碼<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" autoFocus /></label>
        {totp && (
          <label className="field">
            驗證碼
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="驗證器 App 上的 6 位數"
            />
          </label>
        )}
        {error && <div className="error-text">{error}</div>}
        <button className="btn primary" type="submit" disabled={config.isLoading}>登入</button>
      </form>
    </div>
  );
}
