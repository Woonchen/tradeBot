import { useState } from "react";
import { api, setToken } from "../api/client";

export function Login() {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      const { token } = await api<{ token: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setToken(token);
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <h1>TradeBot 登入</h1>
        <label className="field">帳號<input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label className="field">密碼<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus /></label>
        {error && <div className="error-text">{error}</div>}
        <button className="btn primary" type="submit">登入</button>
      </form>
    </div>
  );
}
