// Address of the backend on Render. Change after the first Render deploy, then redeploy on Vercel.
// For a one-off test you can also open the page with ?backend=https://your-service.onrender.com
window.APP_CONFIG = {
  BACKEND_URL: "https://voice-agent-backend-106z.onrender.com",
  // GPT-Live bills every open second. A pause closes the session after this many seconds
  // (not while Єва waits for «так/ні» or runs a tool); silence this long during a call pauses her.
  PAUSE_CLOSE_SECONDS: 30,
  IDLE_PAUSE_SECONDS: 90,
};
