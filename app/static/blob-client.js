(function exposeBlobClient(global) {
  "use strict";

  const BLOB_API = "https://vercel.com/api/blob";
  const API_VERSION = "12";

  function storeIdFromToken(token) {
    const parts = String(token || "").split("_");
    if (parts.length < 5 || parts[0] !== "vercel" || parts[1] !== "blob" || parts[2] !== "client") {
      throw new Error("服务端返回了无效的大文件上传令牌。");
    }
    return parts[3];
  }

  async function upload(file, options = {}) {
    if (!(file instanceof Blob) || !file.size) throw new Error("没有选择可上传的文件。");
    const pathname = options.pathname;
    const handleUploadUrl = options.handleUploadUrl || "/api/blob/upload-token";
    if (!pathname) throw new Error("大文件上传路径缺失。");

    const tokenResponse = await fetch(handleUploadUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Xingxiaodao-Upload": "1" },
      body: JSON.stringify({
        type: "blob.generate-client-token",
        payload: {
          pathname,
          multipart: false,
          clientPayload: JSON.stringify({
            filename: file.name,
            sizeBytes: file.size,
            contentType: file.type || "application/octet-stream",
          }),
        },
      }),
    });
    if (!tokenResponse.ok) throw new Error(await responseError(tokenResponse));
    const tokenPayload = await tokenResponse.json();
    const clientToken = tokenPayload.clientToken;
    const storeId = storeIdFromToken(clientToken);
    const requestId = `${storeId}:${Date.now()}:${crypto.randomUUID()}`;

    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const target = `${BLOB_API}/?pathname=${encodeURIComponent(pathname)}`;
      xhr.open("PUT", target, true);
      xhr.setRequestHeader("Authorization", `Bearer ${clientToken}`);
      xhr.setRequestHeader("x-api-version", API_VERSION);
      xhr.setRequestHeader("x-api-blob-request-id", requestId);
      xhr.setRequestHeader("x-api-blob-request-attempt", "0");
      xhr.setRequestHeader("x-vercel-blob-store-id", storeId);
      xhr.setRequestHeader("x-vercel-blob-access", "public");
      xhr.setRequestHeader("x-content-type", file.type || "application/octet-stream");
      xhr.setRequestHeader("x-content-length", String(file.size));
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && options.onProgress) {
          options.onProgress(Math.round((event.loaded / event.total) * 100));
        }
      });
      xhr.addEventListener("error", () => reject(new Error("大文件上传网络失败，请稍后重试。")));
      xhr.addEventListener("abort", () => reject(new Error("大文件上传已取消。")));
      xhr.addEventListener("load", () => {
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(`大文件上传失败（HTTP ${xhr.status}）。`));
          return;
        }
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error("大文件上传返回了无法解析的响应。"));
        }
      });
      xhr.send(file);
    });
  }

  async function responseError(response) {
    try {
      const payload = await response.json();
      return payload.detail || payload.error || `HTTP ${response.status}`;
    } catch {
      return `HTTP ${response.status}`;
    }
  }

  global.XingxiaodaoBlob = { upload };
})(window);
