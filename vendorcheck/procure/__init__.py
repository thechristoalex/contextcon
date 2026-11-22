"""Three procurement services modelled on Zoho Procurement, behind one HTTP router (api.py).

  onboarding.VendorOnboardingService   Vendors + Vendor Portal sign-up      (State pattern)
  invoices.InvoiceService              Uploaded invoices -> Bills           (Chain of Responsibility + State)
  payables.PayablesService             Payments Made, paid exactly once     (Command + Decorator + State)

They share one SQLite file but own separate tables, and talk through domain events on core.BUS
(Observer), so each can be split into its own process later without changing its code paths.
"""
