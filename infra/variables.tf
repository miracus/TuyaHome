variable "name" {
  description = "Префікс усіх ресурсів"
  type        = string
  default     = "tuyahome"
}

variable "region" {
  description = "Акаунт нової схеми AWS працює лише в одному регіоні"
  type        = string
  default     = "eu-north-1"
}

variable "tuya_access_id" {
  type = string
}

variable "tuya_access_secret" {
  type      = string
  sensitive = true
}

variable "tuya_endpoint" {
  type    = string
  default = "https://openapi.tuyaeu.com"
}

variable "tuya_uid" {
  description = "UID акаунта Smart Life"
  type        = string
}

variable "web_users" {
  description = "Користувачі дашборда; кожен отримує своє посилання. Прибрати ім'я = вимкнути доступ"
  type        = list(string)
  default     = ["owner", "member"]
}

variable "telegram_bot_token" {
  description = "Порожньо — сповіщення лише в лог"
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  type    = string
  default = ""
}

variable "monthly_budget_usd" {
  description = "Місячний бюджет AWS, USD: понад нього (факт або прогноз) — сигнал власнику в Telegram"
  type        = string
  default     = "1"
}
