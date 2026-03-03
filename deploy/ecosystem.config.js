/**
 * NOC Sentinel — PM2 Ecosystem Config
 * Manages the WhatsApp Node.js bridge service.
 *
 * Usage:
 *   pm2 start deploy/ecosystem.config.js
 *   pm2 save
 *   pm2 startup   ← follow the printed command to enable auto-start on boot
 *
 * Monitor:
 *   pm2 status
 *   pm2 logs noc-whatsapp
 *   pm2 monit
 */

module.exports = {
  apps: [
    {
      name: "noc-whatsapp",
      script: "index.js",
      cwd: "/opt/noc-sentinel/whatsapp",

      // Restart policy
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 5000,       // 5 s between restarts
      min_uptime: "10s",         // must stay up 10s to count as a successful start

      // Environment
      env: {
        NODE_ENV: "production",
        PORT: 3001,
      },

      // Logging
      out_file: "/var/log/noc-sentinel/whatsapp-out.log",
      error_file: "/var/log/noc-sentinel/whatsapp-err.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      max_size: "50M",           // rotate at 50 MB
      retain: 10,                // keep last 10 rotated files

      // Single instance (WhatsApp Web session is not cluster-safe)
      instances: 1,
      exec_mode: "fork",
    },
  ],
};
