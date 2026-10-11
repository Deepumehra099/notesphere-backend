const nodemailer = require('nodemailer');

const transporter = nodemailer.createTransport({
  service: 'gmail',
  auth: {
    user: process.env.EMAIL_USER,
    pass: process.env.EMAIL_PASS,
  },
});

async function sendOTP(email, otp) {
  if (!process.env.EMAIL_USER || !process.env.EMAIL_PASS) {
    throw new Error('Email credentials are missing in environment variables');
  }

  const mailOptions = {
    from: process.env.EMAIL_USER,
    to: email,
    subject: 'Your NotesSphere OTP Code',
    text: `Your OTP for NotesSphere is ${otp}. It expires in 5 minutes.`,
    html: `
      <div style="font-family: Arial, sans-serif; padding: 24px; background: #f8fafc; color: #0f172a;">
        <h2 style="margin-bottom: 12px;">NotesSphere Verification</h2>
        <p style="margin-bottom: 18px;">Use the OTP below to continue:</p>
        <div style="font-size: 28px; font-weight: 700; letter-spacing: 6px; margin-bottom: 18px;">${otp}</div>
        <p style="margin: 0;">This code expires in 5 minutes.</p>
      </div>
    `,
  };

  return transporter.sendMail(mailOptions);
}

module.exports = {
  sendOTP,
};
