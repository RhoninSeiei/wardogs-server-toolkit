FROM python:3.11-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY announcement_policy.py player_chats.py crash_advisory.py restart_unban.py temporary_notice.py unknown_handoff.py player_sanction_summary.py admin_bans.py player_kills.py current_restrictions.py punishment_history.py steam_profiles.py kill_accounting.py admin_unban.py admin_tools.py admin_cycle.py recent_kills.py welcome_delivery.py ban_statistics.py helicopter_fun.py burst_guard.py player_history.py admin_history.py match_query.py behavior_policy.py kill_cap.py kick_guard.py language_spacing.py network_announcements.py app.py announcements.py rcon.py moderation_logic.py moderation_ops.py config.json openapi.json /app/
USER 1000:1000
CMD ["python", "/app/app.py"]
