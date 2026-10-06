-- Одобрение приватного адреса привязано к набору IP (docs/contracts.md, раздел 11): админ одобряет то, во что имя
-- разрешалось при одобрении; если DNS отдаёт другой приватный адрес, нужно повторное одобрение.
-- Строки с allow_private, одобренные до этой миграции, набора не имеют: адрес из SQL не разрешить. Они ждут повторного
-- одобрения (статус pending_admin, флаг остаётся) и сразу видны в GET /api/admin/provider-requests.
alter table bothub.providers add column allow_private_ips text[] not null default '{}';
update bothub.providers
   set status = 'pending_admin', last_check_at = null, last_error = 'адрес изменился, нужно повторное одобрение'
 where allow_private and status <> 'disabled';
