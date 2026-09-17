module.exports = {
  apps: [{
    name: "rcon-gateway",
    cwd: __dirname,
    script: "./start_gateway.sh",
    interpreter: "/bin/bash",
    autorestart: true,
    restart_delay: 3000,
    watch: false,
    max_memory_restart: "256M",
    time: true
  }]
};
