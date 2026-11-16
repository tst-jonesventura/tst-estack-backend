"""
Email service implementation for passwordless authentication.
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from pydantic import EmailStr

from utils import log

from utils.config import settings

logger = log.setup_custom_logger(__name__)


class EmailService:
    """
    Implementation of email service for sending authentication codes.
    """

    def __init__(self):
        self.email_host = settings.email_host
        self.email_user = settings.email_user
        self.email_password = settings.email_password
        self.email_port = 587  # Standard SMTP port for TLS
        self.estack_front_host = settings.estack_front_host

    def send_authentication_code(self, email: EmailStr, code: str) -> bool:
        """
        Send an authentication code to an email address via SMTP.

        Args:
            email: Email address to send the code to
            code: Authentication code to send

        Returns:
            True if the email was sent successfully, False otherwise
        """

        try:
            logger.info("Attempting to send authentication code to: %s", email)

            msg = MIMEMultipart()
            msg["From"] = self.email_user
            msg["To"] = email
            msg["Subject"] = "Seu Código de Autenticação para o E-Stack"

            body = f"""
            O seu código de autenticação para acessar o E-Stack é: {code}

            Este código expirará em alguns minutos.

            Se você não solicitou este código, ignore este e-mail.
            """

            msg.attach(MIMEText(body, "plain"))

            server = smtplib.SMTP(self.email_host, self.email_port)
            server.starttls()
            server.login(self.email_user, self.email_password)
            text = msg.as_string()
            server.sendmail(self.email_user, email, text)
            server.quit()

            logger.info("Successfully sent authentication code to: %s", email)
            return True

        except Exception as e:
            logger.error(
                "Failed to send authentication code to %s: %s",
                email,
                str(e),
                exc_info=True,
            )
            print(f"[Console - Email failed]: Code {code} sent to {email}")
            return False

    def send_invite_notification(self, email: EmailStr, project_name: str) -> bool:
        """
        Send an invite notification to an email address via SMTP.

        Args:
            email: Email address to send the code to
            project_name: Name of the project for the invitation

        Returns:
            True if the email was sent successfully, False otherwise
        """

        try:
            logger.info("Attempting to send invite notification to: %s", email)

            msg = MIMEMultipart()
            msg["From"] = self.email_user
            msg["To"] = email
            msg["Subject"] = f"""Acesso ao projeto {project_name} no e-Stack"""

            body = f"""
            <html lang="pt-BR">
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333333;">
                <div style="max-width: 600px; margin: 20px auto; padding: 20px; border: 1px solid #dddddd; border-radius: 8px;">
                    <p>Você foi adicionado(a) como <b>colaborador(a)</b> no projeto <b>{project_name}</b>.</p>
                    <p>
                        Para acessar o <b>e-Stack</b>, clique no botão abaixo e faça login utilizando o seu e-mail.<br>
                        Em seguida, você receberá um código de 4 dígitos para confirmar o acesso.
                    </p>
                    <div style="text-align: center; margin: 30px 0;">
                        <a href="{self.estack_front_host}/login" style="background-color: #1a1a1a; color: #ffffff; padding: 12px 25px; text-decoration: none; border-radius: 5px; display: inline-block; font-weight: bold; font-size: 16px; border: 1px solid #1a1a1a;">
                            Acessar {project_name} no e-Stack
                        </a>
                    </div>
                    <p style="margin-top: 20px; margin-bottom: 0;">
                        Atenciosamente,<br>
                        <b>e-Stack</b>
                    </p>
                </div>
                <p style="text-align: center; font-size: 12px; color: #aaaaaa; margin-top: 20px;">
                    O botão não está funcionando? Cole o seguinte link no seu navegador: <br>
                    <a href="{self.estack_front_host}/login" style="background-color:rgba(0,0,0,0);box-sizing:border-box;color:#0366d6;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,Helvetica,Arial,sans-serif,&quot;Apple Color Emoji&quot;,&quot;Segoe UI Emoji&quot;!important" target="_blank">{self.estack_front_host}/login</a><br><br>
                    Este é um e-mail automático. Por favor, não responda.
                </p>
            </body>
            </html>
            """

            msg.attach(MIMEText(body, "html"))

            server = smtplib.SMTP(self.email_host, self.email_port)
            server.starttls()
            server.login(self.email_user, self.email_password)
            server.sendmail(self.email_user, email, msg.as_string())
            server.quit()

            logger.info("Successfully sent invite notification to: %s", email)
            return True

        except Exception as e:
            logger.error(
                "Failed to send invite notification to %s: %s",
                email,
                str(e),
                exc_info=True,
            )
            print(
                f"[Console - Email failed]: Failed to send invite notification to {email}"
            )
            return False


email_service = EmailService()
