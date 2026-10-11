const nodemailer = require("nodemailer");
const env = require("../config/env");

let transporter = null;

function getTransporter() {
  if (!env.emailUser || !env.emailPass) {
    return null;
  }

  if (!transporter) {
    transporter = nodemailer.createTransport({
      service: "gmail",
      auth: {
        user: env.emailUser,
        pass: env.emailPass,
      },
    });
  }

  return transporter;
}

async function sendEmail({ to, subject, text, html }) {
  const smtp = getTransporter();

  if (!smtp) {
    console.warn("Email transporter not configured; skipping email send.");
    return { skipped: true };
  }

  return smtp.sendMail({
    from: env.emailUser,
    to,
    subject,
    text,
    html,
  });
}

async function sendOtpEmail(email, otp, name = "there") {
  const subject = "Your NotesSphere OTP code";
  const text = `Hi ${name}, your OTP is ${otp}. It expires in 5 minutes.`;
  const html = `
    <div style="font-family:Arial,sans-serif;padding:24px;background:#0b1f13;color:#f7f7f7">
      <h2 style="margin:0 0 12px;color:#f5c451">NotesSphere OTP</h2>
      <p style="margin:0 0 16px">Hi ${name}, use the code below to continue.</p>
      <div style="display:inline-block;padding:14px 18px;border-radius:14px;background:#10351f;border:1px solid #d6b14d;font-size:28px;font-weight:700;letter-spacing:6px">${otp}</div>
      <p style="margin:16px 0 0;color:#b8c7bd">This code expires in 5 minutes.</p>
    </div>
  `;

  return sendEmail({ to: email, subject, text, html });
}

module.exports = { sendEmail, sendOtpEmail };
