# --- Сторожовий пес (21.11): сигнали CloudWatch і бюджету → SNS → Lambda watchdog → Telegram власнику.
# Бот і опитувач не можуть самі сказати, що вони «лежать» (19.11 вичерпано ліміт 10 одночасних Lambda — бот мовчав).

resource "aws_sns_topic" "watchdog" {
  name = "${var.name}-watchdog"
}

# бюджет AWS публікує в тему від імені budgets.amazonaws.com
data "aws_iam_policy_document" "watchdog_topic" {
  statement {
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.watchdog.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com", "cloudwatch.amazonaws.com"]
    }
  }
}

resource "aws_sns_topic_policy" "watchdog" {
  arn    = aws_sns_topic.watchdog.arn
  policy = data.aws_iam_policy_document.watchdog_topic.json
}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/${var.name}-watchdog"
  retention_in_days = 14
}

data "aws_iam_policy_document" "watchdog" {
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.main.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.watchdog.arn}:*"]
  }
}

resource "aws_iam_role" "watchdog" {
  name               = "${var.name}-watchdog"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

resource "aws_iam_role_policy" "watchdog" {
  name   = "${var.name}-watchdog"
  role   = aws_iam_role.watchdog.id
  policy = data.aws_iam_policy_document.watchdog.json
}

resource "aws_lambda_function" "watchdog" {
  function_name    = "${var.name}-watchdog"
  role             = aws_iam_role.watchdog.arn
  runtime          = "python3.13"
  handler          = "watchdog.lambda_handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  timeout          = 20
  memory_size      = 128
  environment {
    variables = merge(local.app_env, {
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
      TELEGRAM_CHAT_ID   = var.telegram_chat_id
    })
  }
  depends_on = [aws_cloudwatch_log_group.watchdog, aws_iam_role_policy.watchdog]
}

resource "aws_lambda_permission" "watchdog_sns" {
  statement_id  = "AllowSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.watchdog.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.watchdog.arn
}

resource "aws_sns_topic_subscription" "watchdog" {
  topic_arn = aws_sns_topic.watchdog.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.watchdog.arn
}

# --- Сигнали. Текст для людини — у alarm_description (його й надсилає watchdog).
locals {
  alarms = {
    poller_silent = {
      description = "опитувач не запускався 10 хв — стан дому не оновлюється, сповіщень не буде"
      function    = aws_lambda_function.poller.function_name
      metric      = "Invocations", stat = "Sum", period = 300, periods = 2, op = "LessThanThreshold", threshold = 1
      missing     = "breaching"
    }
    poller_errors = {
      description = "опитувач падає з помилкою вже 15 хв поспіль"
      function    = aws_lambda_function.poller.function_name
      metric      = "Errors", stat = "Sum", period = 300, periods = 3, op = "GreaterThanOrEqualToThreshold", threshold = 1
      missing     = "notBreaching"
    }
    poller_throttles = {
      description = "опитувач відхилено через ліміт одночасних Lambda (10 на акаунт)"
      function    = aws_lambda_function.poller.function_name
      metric      = "Throttles", stat = "Sum", period = 300, periods = 1, op = "GreaterThanOrEqualToThreshold", threshold = 1
      missing     = "notBreaching"
    }
    web_throttles = {
      description = "бот/дашборд відхилено через ліміт одночасних Lambda — бот може не відповідати"
      function    = aws_lambda_function.web.function_name
      metric      = "Throttles", stat = "Sum", period = 300, periods = 1, op = "GreaterThanOrEqualToThreshold", threshold = 1
      missing     = "notBreaching"
    }
    web_errors = {
      description = "бот/дашборд падає з помилкою (≥ 3 за 5 хв)"
      function    = aws_lambda_function.web.function_name
      metric      = "Errors", stat = "Sum", period = 300, periods = 1, op = "GreaterThanOrEqualToThreshold", threshold = 3
      missing     = "notBreaching"
    }
    web_slow = {
      description = "бот/дашборд відповідає повільно (у середньому > 3 с 10 хв поспіль)"
      function    = aws_lambda_function.web.function_name
      metric      = "Duration", stat = "Average", period = 300, periods = 2, op = "GreaterThanThreshold", threshold = 3000
      missing     = "notBreaching"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "watchdog" {
  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = "AWS/Lambda"
  metric_name         = each.value.metric
  dimensions          = { FunctionName = each.value.function }
  statistic           = each.value.stat
  period              = each.value.period
  evaluation_periods  = each.value.periods
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  treat_missing_data  = each.value.missing
  alarm_actions       = [aws_sns_topic.watchdog.arn]
  ok_actions          = [aws_sns_topic.watchdog.arn]
}

# --- Бюджет: фактичні або прогнозовані витрати за місяць понад var.monthly_budget_usd
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.watchdog.arn]
  }
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.watchdog.arn]
  }
  depends_on = [aws_sns_topic_policy.watchdog]
}
