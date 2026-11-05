FROM alpine:3.20
RUN apk add --no-cache samba
RUN adduser -D -H -s /sbin/nologin reader && printf 'readerpass\nreaderpass\n' | smbpasswd -a -s reader
RUN printf '%s\n' '[global]' 'map to guest = never' 'server min protocol = SMB2' 'log file = /dev/stdout' \
  'dns proxy = no' 'load printers = no' '[ops]' 'path = /share' 'read only = yes' 'valid users = reader' 'browseable = yes' \
  > /etc/samba/smb.conf
EXPOSE 445
CMD ["smbd", "--foreground", "--no-process-group", "--debug-stdout"]
