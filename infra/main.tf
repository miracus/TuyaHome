# TuyaHome — опитувач Tuya + дашборд. Усі ресурси з префіксом var.name; стан Terraform локальний (infra/terraform.tfstate).
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
    random  = { source = "hashicorp/random", version = "~> 3.7" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name }
  }
}

locals {
  cfg          = jsondecode(file("${path.module}/../app/config.json"))
  snapshot_min = local.cfg.polling.snapshot_every_min
  app_env = {
    TABLE_NAME = aws_dynamodb_table.main.name
  }
}

data "archive_file" "app" {
  type        = "zip"
  source_dir  = "${path.module}/../app"
  output_path = "${path.module}/build/app.zip"
  excludes    = ["__pycache__"]
}

# Telethon (заявки в ДТЕК від акаунта власника) — окремим шаром лише для опитувача.
# Збирання: python -m pip install telethon --target infra/build/layer/python --no-compile
data "archive_file" "telethon" {
  type        = "zip"
  source_dir  = "${path.module}/build/layer"
  output_path = "${path.module}/build/telethon_layer.zip"
  excludes    = ["python/bin"]
}

resource "aws_lambda_layer_version" "telethon" {
  layer_name          = "${var.name}-telethon"
  filename            = data.archive_file.telethon.output_path
  source_code_hash    = data.archive_file.telethon.output_base64sha256
  compatible_runtimes = ["python3.13"]
}

data "aws_caller_identity" "me" {}

# Окремий ключ (посилання) для кожного користувача — доступ вимикається окремо, прибравши ім'я з var.web_users
resource "random_password" "web_key" {
  for_each = toset(var.web_users)
  length   = 32
  special  = false
}

# --- DynamoDB: одна таблиця, provisioned у межах always-free (25 RCU / 25 WCU)
resource "aws_dynamodb_table" "main" {
  name           = var.name
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 10
  hash_key       = "pk"
  range_key      = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "exp"
    enabled        = true
  }
}

# --- IAM: роль для обох Lambda — лише своя таблиця і свої логи
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:BatchWriteItem"]
    resources = [aws_dynamodb_table.main.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.poller.arn}:*", "${aws_cloudwatch_log_group.web.arn}:*"]
  }
  # ключ входу Telegram власника (створює tools/dtek_tg.py login; ключ шифрування — aws/ssm)
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter${local.cfg.dtek_report.ssm_param}"]
  }
  # кнопка «🆘 Заявка ДТЕК»: web-Lambda запускає опитувача асинхронно (ім'я — рядком, щоб не було циклу залежностей);
  # пилосос: бот, дашборд і опитувач запускають Lambda пилососа асинхронно
  statement {
    actions = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.me.account_id}:function:${var.name}-poller",
    "arn:aws:lambda:${var.region}:${data.aws_caller_identity.me.account_id}:function:${var.name}-vacuum"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_cloudwatch_log_group" "poller" {
  name              = "/aws/lambda/${var.name}-poller"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/${var.name}-web"
  retention_in_days = 14
}

# --- Опитувач
resource "aws_lambda_function" "poller" {
  function_name    = "${var.name}-poller"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.13"
  handler          = "poller.lambda_handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  layers           = [aws_lambda_layer_version.telethon.arn]
  timeout          = 60
  memory_size      = 256 # Telethon не вміщується в 128 МБ (16.11: таймаут при 128/128)
  environment {
    variables = merge(local.app_env, {
      TUYA_ACCESS_ID     = var.tuya_access_id
      TUYA_ACCESS_SECRET = var.tuya_access_secret
      TUYA_ENDPOINT      = var.tuya_endpoint
      TUYA_UID           = var.tuya_uid
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
      # для реєстрації webhook бота (poller {"tg_setup": true})
      TG_SECRET = random_password.tg_secret.result
      WEB_URL   = aws_lambda_function_url.web.function_url
    }, local.vacuum_env)
  }
  depends_on = [aws_cloudwatch_log_group.poller, aws_iam_role_policy.lambda]
}

# секрет webhook-а Telegram: Telegram додає його в заголовок кожного запиту, чужі запити відкидаються
resource "random_password" "tg_secret" {
  length  = 40
  special = false
}

resource "aws_cloudwatch_event_rule" "poll" {
  name                = "${var.name}-poll"
  schedule_expression = local.snapshot_min == 1 ? "rate(1 minute)" : "rate(${local.snapshot_min} minutes)"
}

resource "aws_cloudwatch_event_target" "poll" {
  rule = aws_cloudwatch_event_rule.poll.name
  arn  = aws_lambda_function.poller.arn
}

resource "aws_lambda_permission" "poll" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.poll.arn
}

# --- Дашборд (Function URL — одразу https, CloudFront не потрібен)
resource "aws_lambda_function" "web" {
  function_name    = "${var.name}-web"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.13"
  handler          = "web.lambda_handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  timeout          = 30
  memory_size      = 512 # 19.11: на 128 МБ запит дашборда йшов 6–7 с і займав слот з ліміту 10 одночасних (бот не відповідав)
  environment {
    variables = merge(local.app_env, {
      POLLER_FN          = "${var.name}-poller"
      WEB_KEYS           = jsonencode({ for u, p in random_password.web_key : u => p.result })
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
      TG_SECRET          = random_password.tg_secret.result
    }, local.vacuum_env)
  }
  depends_on = [aws_cloudwatch_log_group.web, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function_url" "web" {
  function_name      = aws_lambda_function.web.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "web_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.web.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
