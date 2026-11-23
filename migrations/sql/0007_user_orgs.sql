-- The dashboard lists the organizations the signed-in user belongs to. Memberships are tenant-scoped by
-- RLS, so this one read crosses tenants through a narrow SECURITY DEFINER function: it returns only the
-- memberships of the user id it is given (the API passes the authenticated user's id), and only the
-- organization's id, slug, name and the user's role.
CREATE FUNCTION app_user_orgs(p_user_id uuid)
    RETURNS TABLE (org_id uuid, slug text, name text, role text)
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$
    SELECT o.id, o.slug, o.name, m.role
    FROM memberships m JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id AND o.status = 'active'
    ORDER BY o.name
$$;

REVOKE ALL ON FUNCTION app_user_orgs(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_user_orgs(uuid) TO aieos_app;
