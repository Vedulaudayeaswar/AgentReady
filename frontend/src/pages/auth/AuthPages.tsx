import { useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { CheckCircle2 } from "lucide-react";
import { ErrorNote, Logo, ThemeToggle } from "@/components/app/common";
import { Button, Field, Input } from "@/components/ui/primitives";
import { useAuth } from "@/hooks/useAuth";
import { api } from "@/services/api";

const password = z
  .string()
  .min(10, "At least 10 characters")
  .regex(/[A-Za-z]/, "Include a letter")
  .regex(/\d/, "Include a number");

function AuthShell({ title, subtitle, children, footer }: { title: string; subtitle?: ReactNode; children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col bg-muted/30">
      <header className="flex h-14 items-center justify-between px-6">
        <Logo />
        <ThemeToggle />
      </header>
      <div className="flex flex-1 items-center justify-center px-4 pb-16">
        <div className="w-full max-w-[380px]">
          <div className="rounded-xl border bg-card p-7 shadow-sm">
            <h1 className="text-lg font-semibold">{title}</h1>
            {subtitle && <p className="mt-1 text-[13px] text-muted-foreground">{subtitle}</p>}
            <div className="mt-6">{children}</div>
          </div>
          {footer && <p className="mt-5 text-center text-[13px] text-muted-foreground">{footer}</p>}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ login */
const loginSchema = z.object({ email: z.string().email("Enter a valid email"), password: z.string().min(1, "Required") });

export function Login() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [error, setError] = useState<unknown>(null);
  const form = useForm<z.infer<typeof loginSchema>>({ resolver: zodResolver(loginSchema) });
  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    try {
      const { access_token } = await api.login(v);
      await signIn(access_token);
      navigate((location.state as { from?: string } | null)?.from ?? "/app", { replace: true });
    } catch (e) {
      setError(e);
    }
  });
  const { errors, isSubmitting } = form.formState;
  return (
    <AuthShell title="Sign in to AgentReady" footer={<>New here? <Link className="text-foreground underline-offset-4 hover:underline" to="/signup">Create an account</Link></>}>
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <ErrorNote error={error} />
        <Field label="Email" htmlFor="email" error={errors.email?.message}>
          <Input id="email" type="email" autoComplete="email" autoFocus {...form.register("email")} aria-invalid={!!errors.email} />
        </Field>
        <Field
          label={
            <span className="flex w-full items-center justify-between">
              Password
              <Link to="/forgot-password" className="text-[12px] font-normal text-muted-foreground hover:text-foreground">Forgot password?</Link>
            </span>
          }
          htmlFor="password"
          error={errors.password?.message}
        >
          <Input id="password" type="password" autoComplete="current-password" {...form.register("password")} aria-invalid={!!errors.password} />
        </Field>
        <Button type="submit" className="w-full" loading={isSubmitting}>Sign in</Button>
      </form>
    </AuthShell>
  );
}

/* ------------------------------------------------------------------ signup */
const signupSchema = z.object({
  full_name: z.string().max(200).optional(),
  business_name: z.string().min(1, "Required").max(200),
  email: z.string().email("Enter a valid email"),
  password,
});

export function Signup() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);
  const form = useForm<z.infer<typeof signupSchema>>({ resolver: zodResolver(signupSchema) });
  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    try {
      const { access_token } = await api.register(v);
      await signIn(access_token);
      navigate("/app/connect", { replace: true });
    } catch (e) {
      setError(e);
    }
  });
  const { errors, isSubmitting } = form.formState;
  return (
    <AuthShell
      title="Create your account"
      subtitle="Generate a structured catalog from your existing website."
      footer={<>Already have an account? <Link className="text-foreground underline-offset-4 hover:underline" to="/login">Sign in</Link></>}
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate>
        <ErrorNote error={error} />
        <Field label="Business name" htmlFor="business_name" error={errors.business_name?.message}>
          <Input id="business_name" autoFocus {...form.register("business_name")} aria-invalid={!!errors.business_name} />
        </Field>
        <Field label="Your name" htmlFor="full_name">
          <Input id="full_name" autoComplete="name" {...form.register("full_name")} />
        </Field>
        <Field label="Work email" htmlFor="email" error={errors.email?.message}>
          <Input id="email" type="email" autoComplete="email" {...form.register("email")} aria-invalid={!!errors.email} />
        </Field>
        <Field label="Password" htmlFor="password" error={errors.password?.message} hint="At least 10 characters with letters and numbers.">
          <Input id="password" type="password" autoComplete="new-password" {...form.register("password")} aria-invalid={!!errors.password} />
        </Field>
        <Button type="submit" className="w-full" loading={isSubmitting}>Create account</Button>
      </form>
    </AuthShell>
  );
}

/* ------------------------------------------------------------------ forgot */
export function ForgotPassword() {
  const [sent, setSent] = useState<{ message: string; dev_reset_url?: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const form = useForm<{ email: string }>({ resolver: zodResolver(z.object({ email: z.string().email("Enter a valid email") })) });
  const onSubmit = form.handleSubmit(async ({ email }) => {
    setError(null);
    try {
      setSent(await api.forgotPassword(email));
    } catch (e) {
      setError(e);
    }
  });
  return (
    <AuthShell title="Reset your password" subtitle="We'll send a reset link to your email." footer={<Link to="/login" className="hover:text-foreground">Back to sign in</Link>}>
      {sent ? (
        <div className="space-y-3 text-[13px]">
          <p className="flex items-start gap-2"><CheckCircle2 className="mt-0.5 h-4 w-4 text-success" />{sent.message}</p>
          {sent.dev_reset_url && (
            <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-[12.5px]">
              <p className="font-medium">Development mode</p>
              <p className="mt-1 text-muted-foreground">Email delivery is not configured. Use this link:</p>
              <a className="mt-1 block break-all font-mono text-[11.5px] underline" href={sent.dev_reset_url}>{sent.dev_reset_url}</a>
            </div>
          )}
        </div>
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <ErrorNote error={error} />
          <Field label="Email" htmlFor="email" error={form.formState.errors.email?.message}>
            <Input id="email" type="email" autoFocus {...form.register("email")} />
          </Field>
          <Button type="submit" className="w-full" loading={form.formState.isSubmitting}>Send reset link</Button>
        </form>
      )}
    </AuthShell>
  );
}

/* ------------------------------------------------------------------ reset */
export function ResetPassword() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") ?? "";
  const [error, setError] = useState<unknown>(null);
  const schema = z.object({ password, confirm: z.string() }).refine((v) => v.password === v.confirm, { message: "Passwords do not match", path: ["confirm"] });
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema) });
  const onSubmit = form.handleSubmit(async (v) => {
    setError(null);
    try {
      await api.resetPassword(token, v.password);
      navigate("/login", { replace: true });
    } catch (e) {
      setError(e);
    }
  });
  const { errors, isSubmitting } = form.formState;
  return (
    <AuthShell title="Choose a new password">
      {!token ? (
        <ErrorNote error="This reset link is missing its token." />
      ) : (
        <form onSubmit={onSubmit} className="space-y-4" noValidate>
          <ErrorNote error={error} />
          <Field label="New password" htmlFor="password" error={errors.password?.message}>
            <Input id="password" type="password" autoComplete="new-password" autoFocus {...form.register("password")} />
          </Field>
          <Field label="Confirm password" htmlFor="confirm" error={errors.confirm?.message}>
            <Input id="confirm" type="password" autoComplete="new-password" {...form.register("confirm")} />
          </Field>
          <Button type="submit" className="w-full" loading={isSubmitting}>Update password</Button>
        </form>
      )}
    </AuthShell>
  );
}
