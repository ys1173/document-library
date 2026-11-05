{{- define "doclib.visionOn" -}}
{{- if or (eq (toString .Values.vision.mode) "local") (eq (toString .Values.vision.mode) "remote") -}}true{{- end -}}
{{- end -}}

{{- define "doclib.pod" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
containers:
  - name: {{ .name }}
    image: "{{ .root.Values.image.repository }}:{{ .root.Values.image.tag }}"
    imagePullPolicy: {{ .root.Values.image.pullPolicy }}
    args: {{ toJson .args }}
    env:
      - name: DOCLIB_CONFIG
        value: /etc/doclib/config.yaml
      - name: DOCLIB_POSTGRES__DSN_FILE
        value: /run/secrets/dsn/dsn
      {{- if include "doclib.visionOn" .root }}
      - name: DOCLIB_EXTRACTION__VLM__ENABLED
        value: "true"
      {{- end }}
      {{- range $name, $value := .extraEnv }}
      - name: {{ $name }}
        value: {{ $value | quote }}
      {{- end }}
    {{- if and .root.Values.secrets.connectorEnv (not .noCredentials) }}
    envFrom:
      - secretRef: {name: {{ .root.Values.secrets.connectorEnv }}}
    {{- end }}
    {{- with .ports }}
    ports: {{ toJson . }}
    {{- end }}
    {{- with .resources }}
    resources: {{ toJson . }}
    {{- end }}
    securityContext:
      readOnlyRootFilesystem: true
      allowPrivilegeEscalation: false
      capabilities: {drop: [ALL]}
    volumeMounts:
      - {name: config, mountPath: /etc/doclib, readOnly: true}
      - {name: dsn, mountPath: /run/secrets/dsn, readOnly: true}
      - {name: rclone, mountPath: /run/secrets/rclone, readOnly: true}
      - {name: tmp, mountPath: /tmp}
      - {name: home, mountPath: /home/doclib}
volumes:
  - name: config
    configMap: {name: doclib-config}
  - name: dsn
    secret: {secretName: {{ .dsn }}}
  - name: rclone
    secret: {secretName: {{ .root.Values.secrets.rcloneConfig }}, optional: true}
  - name: tmp
    emptyDir: {medium: Memory}
  - name: home
    emptyDir: {medium: Memory}
{{- end -}}
